/* Paired LAN audio and framed JSON v2. Motion is validated by tars_protocol. */
#include "tars_wifi_audio.h"
#include "tars_audio.h"
#include "tars_state.h"
#include "tars_display.h"
#include "tars_motion.h"
#include <string.h>
#include <stdatomic.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include "esp_event.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_random.h"
#include "esp_mac.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "mbedtls/md.h"
#include "lwip/sockets.h"
#include "lwip/tcp.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#define LIMIT 1024
#define HEADER 16
enum { HELLO=1, AUTH=2, READY=3, ACK=4, ERROR_FRAME=5, PING=6,
       CAP_START=10, CAP_STOP=11, MIC_PCM=12, PLAY_START=20, PLAY_PCM=21,
       PLAY_END=22, BUFFER_START=23, BUFFER_PCM=24, BUFFER_COMMIT=25,
       STATE=30, TEXT=31, CONTROL_JSON=40, CONTROL_RESULT=41 };
typedef struct { char ssid[33], password[64], host[16], token[65]; uint16_t port; } config_t;
static config_t s_config;
bool tars_wifi_audio_independent(void) { return true; }
bool tars_wifi_audio_endpoint(char *host,size_t hs,char *token,size_t ts) {
 if(!s_config.host[0] || hs<sizeof(s_config.host) || ts<sizeof(s_config.token)) return false;
 memcpy(host,s_config.host,sizeof(s_config.host)); memcpy(token,s_config.token,sizeof(s_config.token)); return true;
}
static atomic_bool s_wifi_up, s_session;
static atomic_uint s_last_rx;
static atomic_uint s_audio_reason, s_capture_frames, s_watchdog_count;
static atomic_int s_audio_error;
static atomic_bool s_buffer_active;
static atomic_int s_buffer_result;
static struct { int16_t *pcm; size_t bytes; uint8_t volume; } s_buffer_job;

static void buffered_play_worker(void *unused) {
    (void)unused;
    esp_err_t err=tars_audio_play_start(s_buffer_job.volume);
    if(err==ESP_OK) {
        err=tars_audio_play_write(s_buffer_job.pcm,s_buffer_job.bytes/2);
        esp_err_t finish=tars_audio_play_finish();
        if(err==ESP_OK) err=finish;
    }
    heap_caps_free(s_buffer_job.pcm);
    s_buffer_job.pcm=NULL;
    tars_calibration_release_audio();
    atomic_store(&s_buffer_result,err);
    atomic_store(&s_buffer_active,false);
    vTaskDelete(NULL);
}
static void audio_fault(unsigned reason, int error) {
    atomic_store(&s_audio_reason,reason); atomic_store(&s_audio_error,error);
}
void tars_wifi_audio_diagnostic(uint32_t *reason, int *error, uint32_t *frames, uint32_t *watchdogs) {
    *reason=atomic_load(&s_audio_reason); *error=atomic_load(&s_audio_error);
    *frames=atomic_load(&s_capture_frames); *watchdogs=atomic_load(&s_watchdog_count);
}
int tars_wifi_audio_rssi(void) {
    wifi_ap_record_t info;
    return esp_wifi_sta_get_ap_info(&info)==ESP_OK ? info.rssi : -127;
}
static const char *TAG = "wifi_audio";
static void (*s_control_rx)(const char *, size_t);
static void (*s_control_lost)(void);
typedef struct { char json[512]; } control_reply_t;
static QueueHandle_t s_control_out;

void tars_wifi_audio_set_control_handlers(void (*receive)(const char *, size_t), void (*disconnect)(void)) {
    s_control_rx=receive; s_control_lost=disconnect;
    if (!s_control_out) s_control_out=xQueueCreate(16,sizeof(control_reply_t));
}
bool tars_wifi_audio_send_control(const char *json) {
    if (!s_control_out || !atomic_load(&s_session) || !json || strlen(json)>=512) return false;
    control_reply_t reply={0}; memcpy(reply.json,json,strlen(json));
    if (xQueueSend(s_control_out,&reply,0)==pdTRUE) return true;
    atomic_store(&s_session,false); /* overflow fails closed, never blocks motion task */
    return false;
}

static uint32_t get32(const uint8_t *p) { return ((uint32_t)p[0]<<24)|((uint32_t)p[1]<<16)|((uint32_t)p[2]<<8)|p[3]; }
static void put32(uint8_t *p, uint32_t x) { p[0]=x>>24; p[1]=x>>16; p[2]=x>>8; p[3]=x; }
static int64_t now_ms(void) { return esp_timer_get_time()/1000; }

static bool valid_config(const char *ssid, const char *password, const char *host, int port, const char *token) {
    struct in_addr ip;
    if (!ssid || !password || !host || !token || !strlen(ssid) || strlen(ssid)>32 ||
        strlen(password)<8 || strlen(password)>63 || strlen(host)>15 ||
        inet_pton(AF_INET, host, &ip)!=1 || port<1024 || port>65535 ||
        strlen(token)<32 || strlen(token)>64) return false;
    for (const char *p=token; *p; ++p) if (!((*p>='0' && *p<='9') || (*p>='a' && *p<='f'))) return false;
    return true; /* require a WPA password and an explicitly generated hex pairing secret */
}

esp_err_t tars_wifi_audio_configure(const char *ssid, const char *password,
                                  const char *host, int port, const char *token) {
    if (!valid_config(ssid,password,host,port,token) || tars_audio_is_busy() ||
        atomic_load(&s_session)) return ESP_ERR_INVALID_ARG;
    nvs_handle_t nvs;
    esp_err_t err=nvs_open("tars_audio",NVS_READWRITE,&nvs);
    if (err!=ESP_OK) return err;
    config_t config={0};
    memcpy(config.ssid,ssid,strlen(ssid)); memcpy(config.password,password,strlen(password));
    memcpy(config.host,host,strlen(host)); memcpy(config.token,token,strlen(token)); config.port=port;
    /* One atomic blob avoids a partially updated Wi-Fi/token profile. */
    err=nvs_set_blob(nvs,"profile",&config,sizeof(config));
    if (err==ESP_OK) err=nvs_commit(nvs);
    nvs_close(nvs);
    memset(&config,0,sizeof(config));
    return err;
}

bool tars_wifi_audio_session_active(void) { return atomic_load(&s_session); }
void tars_wifi_audio_drop_session(void) { atomic_store(&s_session,false); }

static bool all_io(int fd, void *data, size_t size, bool write) {
    uint8_t *p=data;
    while (size) {
        int n=write ? send(fd,p,size,0) : recv(fd,p,size,0);
        if (n<=0) return false; /* bounded socket timeout; no partial-frame replay */
        p+=n; size-=n;
    }
    return true;
}

static bool send_frame(int fd,uint8_t type,uint32_t session,uint32_t *seq,const void *payload,uint16_t length) {
    if (length>LIMIT || *seq==UINT32_MAX) return false;
    uint8_t frame[HEADER+LIMIT]={'T','W','A','1',type,0,(uint8_t)(length>>8),(uint8_t)length};
    put32(frame+8,session); put32(frame+12,(*seq)++);
    if(length) memcpy(frame+HEADER,payload,length);
    return all_io(fd,frame,HEADER+length,true);
}

static bool read_frame(int fd,uint32_t session,uint32_t *seq,uint8_t *type,uint8_t *payload,uint16_t *length) {
    uint8_t h[HEADER];
    if (!all_io(fd,h,HEADER,false) || memcmp(h,"TWA1",4) || h[5] || get32(h+8)!=session ||
        get32(h+12)!=*seq || *seq==UINT32_MAX) return false;
    *length=((uint16_t)h[6]<<8)|h[7]; *type=h[4];
    if (*length>LIMIT || (*length && !all_io(fd,payload,*length,false))) return false;
    ++*seq; atomic_store(&s_last_rx,now_ms());
    return true;
}

static bool proof(const char *role,const uint8_t hello[38],uint32_t session,uint8_t result[32]) {
    uint8_t message[45]; size_t len=strlen(role);
    memcpy(message,role,len); memcpy(message+len,hello,38); put32(message+len+38,session);
    return mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256),
        (const unsigned char*)s_config.token,strlen(s_config.token),message,len+42,result)==0;
}

static bool utf8_text(const uint8_t *s,size_t n) {
    for(size_t i=0;i<n;) {
        uint32_t cp; unsigned follow; uint8_t c=s[i++];
        if(c<0x80) { if(c<32 || c==127) return false; continue; }
        if(c>=0xc2 && c<=0xdf) { cp=c&31; follow=1; }
        else if(c>=0xe0 && c<=0xef) { cp=c&15; follow=2; }
        else if(c>=0xf0 && c<=0xf4) { cp=c&7; follow=3; }
        else return false;
        if(i+follow>n) return false;
        for(unsigned j=0;j<follow;++j) { uint8_t b=s[i++]; if((b&0xc0)!=0x80) return false; cp=(cp<<6)|(b&63); }
        if((follow==1 && cp<128)||(follow==2 && cp<2048)||(follow==3 && cp<65536)||
            cp>0x10ffff||(cp>=0xd800 && cp<=0xdfff)||(cp>=0x80 && cp<=0x9f)) return false;
    }
    return true;
}

static void audio_session(int fd) {
    uint32_t session=esp_random(); if(!session) session=1;
    uint32_t txseq=0,rxseq=0;
    uint8_t hello[38],expected[32],data[LIMIT]; uint8_t type; uint16_t length;
    esp_fill_random(hello,32); esp_read_mac(hello+32,ESP_MAC_WIFI_STA);
    if(!send_frame(fd,HELLO,session,&txseq,hello,sizeof(hello)) ||
       !read_frame(fd,session,&rxseq,&type,data,&length) || type!=AUTH || length!=32) return;
    if(!proof("PC",hello,session,expected)) return;
    uint8_t difference=0; for(int i=0;i<32;++i) difference|=expected[i]^data[i];
    if(difference) return;
    if(!proof("ESP",hello,session,expected)) return;
    if(!send_frame(fd,READY,session,&txseq,expected,32)) return;
    if (s_control_out) xQueueReset(s_control_out);
    atomic_store(&s_last_rx,now_ms()); atomic_store(&s_session,true);
    tars_state_set_audio_connected(true);
    int mode=0; int64_t started=0; size_t played=0; bool reserved=false;
    uint8_t *staged=NULL; size_t staged_size=0,staged_used=0; uint8_t staged_volume=0;
    uint32_t commit_sequence=0;
    /* Coalesce four 8ms I2S blocks. Tiny TCP writes consumed a segment per
     * block; retain control polling every block, transmit PCM every 32ms. */
    int16_t mic_packet[512]; size_t mic_used=0;
    while(atomic_load(&s_wifi_up) && atomic_load(&s_session)) {
        if(mode==4 && !atomic_load(&s_buffer_active)) {
            esp_err_t result=atomic_load(&s_buffer_result);
            uint8_t ack[5]; put32(ack,commit_sequence); ack[4]=result==ESP_OK ? 0 : 1;
            mode=0; tars_state_set_interaction(TARS_INTERACTION_IDLE);
            if(!send_frame(fd,ACK,session,&txseq,ack,sizeof(ack))) { audio_fault(3,errno); break; }
            if(result!=ESP_OK) { audio_fault(125,result); break; }
        }
        control_reply_t reply;
        bool sent=true;
        /* Control replies take priority over microphone data; bounded queue. */
        for(int i=0; s_control_out && i<16 && xQueueReceive(s_control_out,&reply,0)==pdTRUE; ++i)
            if(!send_frame(fd,CONTROL_RESULT,session,&txseq,reply.json,strlen(reply.json))) { sent=false; break; }
        if(!sent) { audio_fault(1,errno); break; }
        fd_set set; FD_ZERO(&set); FD_SET(fd,&set);
        struct timeval wait={.tv_sec=0,.tv_usec=mode==1 ? 0 : 20000};
        int available=select(fd+1,&set,NULL,NULL,&wait);
        if(available<0) { audio_fault(7,errno); break; }
        if(available>0) {
            uint32_t request_seq=rxseq;
            if(!read_frame(fd,session,&rxseq,&type,data,&length)) { audio_fault(2,errno); break; }
            if(!atomic_load(&s_session) || !atomic_load(&s_wifi_up)) break;
            esp_err_t err=ESP_ERR_INVALID_ARG;
            bool deferred_ack=false;
            if(type==PING && !length) err=ESP_OK;
            else if(type==CONTROL_JSON && length && length<=512 && s_control_rx && !memchr(data,0,length)) {
                s_control_rx((const char*)data,length); err=ESP_OK;
            }
            else if(false && type==CAP_START && !length && mode==0 && !tars_motion_is_busy() && !tars_calibration_status().active &&
                    tars_state_get().health!=TARS_HEALTH_ESTOP && tars_state_get().health!=TARS_HEALTH_FAULT) {
                reserved=tars_calibration_reserve_audio();
                err=reserved ? tars_audio_recover() : ESP_ERR_INVALID_STATE;
                if(err==ESP_OK) err=tars_audio_capture_start();
                if(err==ESP_OK) { mode=1; mic_used=0; started=now_ms(); tars_state_set_interaction(TARS_INTERACTION_LISTENING); }
            } else if(type==CAP_STOP && !length && mode==1) {
                tars_audio_capture_stop(); mode=0; err=ESP_OK;
                if(reserved) { tars_calibration_release_audio(); reserved=false; }
            } else if(false && type==PLAY_START && length==1 && data[0]<=20 && mode==0 &&
                      !tars_motion_is_busy() && !tars_calibration_status().active &&
                      tars_state_get().health!=TARS_HEALTH_ESTOP && tars_state_get().health!=TARS_HEALTH_FAULT) {
                reserved=tars_calibration_reserve_audio();
                err=reserved ? tars_audio_recover() : ESP_ERR_INVALID_STATE;
                if(err==ESP_OK) err=tars_audio_play_start(data[0]);
                if(err==ESP_OK) { mode=2; started=now_ms(); played=0; tars_state_set_interaction(TARS_INTERACTION_SPEAKING); }
            } else if(type==PLAY_PCM && mode==2 && length && !(length%2) && played+length<=320000) {
                int16_t pcm[LIMIT/2]; memcpy(pcm,data,length);
                err=tars_audio_play_write(pcm,length/2); played+=length;
            } else if(type==PLAY_END && !length && mode==2) {
                err=tars_audio_play_finish(); mode=0; tars_state_set_interaction(TARS_INTERACTION_IDLE);
                if(reserved) { tars_calibration_release_audio(); reserved=false; }
            } else if(false && type==BUFFER_START && length==5 && mode==0 && data[0]<=20 &&
                      tars_state_get().health!=TARS_HEALTH_ESTOP && tars_state_get().health!=TARS_HEALTH_FAULT &&
                      !atomic_load(&s_buffer_active) && get32(data+1)>0 && get32(data+1)<=256000 && !(get32(data+1)&1)) {
                reserved=tars_calibration_reserve_audio();
                if(reserved) {
                    staged_size=get32(data+1); staged_used=0; staged_volume=data[0];
                    err=tars_audio_recover();
                    if(err==ESP_OK) {
                        staged=heap_caps_malloc(staged_size,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
                        err=staged ? ESP_OK : ESP_ERR_NO_MEM;
                    }
                    if(err==ESP_OK) { mode=3; started=now_ms(); }
                } else err=ESP_ERR_INVALID_STATE;
            } else if(type==BUFFER_PCM && mode==3 && length && !(length&1) && staged_used+length<=staged_size) {
                memcpy(staged+staged_used,data,length); staged_used+=length; err=ESP_OK;
            } else if(type==BUFFER_COMMIT && mode==3 && !length && staged_used==staged_size) {
                err=ESP_OK; /* Never clear an abort that occurred during upload. */
                if(err==ESP_OK) {
                s_buffer_job.pcm=(int16_t*)staged; s_buffer_job.bytes=staged_size; s_buffer_job.volume=staged_volume;
                atomic_store(&s_buffer_active,true);
                if(xTaskCreate(buffered_play_worker,"audio_cached",6144,NULL,4,NULL)==pdPASS) {
                    staged=NULL; reserved=false; /* worker owns memory and audio reservation */
                    mode=4; started=now_ms(); commit_sequence=request_seq; deferred_ack=true; err=ESP_OK;
                    tars_state_set_interaction(TARS_INTERACTION_SPEAKING);
                } else { atomic_store(&s_buffer_active,false); err=ESP_ERR_NO_MEM; }
                }
            } else if(type==STATE && length==1 && data[0]<=3) {
                tars_state_set_interaction((tars_interaction_t)data[0]); err=ESP_OK;
            } else if(type==TEXT && length>=2 && length<=301 && data[0]<=1 && utf8_text(data+1,length-1)) {
                err=tars_display_post_dialogue(data[0] ? "tars" : "user",(char*)data+1,length-1) ? ESP_OK : ESP_FAIL;
            }
            uint8_t ack[5]; put32(ack,request_seq);
            ack[4]=err==ESP_OK ? 0 : err==ESP_ERR_INVALID_STATE ? 2 : err==ESP_ERR_TIMEOUT ? 3 : 1;
            if(!deferred_ack && !send_frame(fd,ACK,session,&txseq,ack,sizeof(ack))) { audio_fault(3,errno); break; }
            if(err!=ESP_OK) { audio_fault(100+type,err); break; }
        }
        if(mode==1) {
            int16_t pcm[128]; size_t count=0;
            size_t capacity=512-mic_used; if(capacity>128) capacity=128;
            esp_err_t capture_error=tars_audio_capture_read(pcm,capacity,&count);
            if(capture_error!=ESP_OK) { audio_fault(4,capture_error); break; }
            atomic_fetch_add(&s_capture_frames,1);
            memcpy(mic_packet+mic_used,pcm,count*2); mic_used+=count;
            if(mic_used>=512) {
                if(!send_frame(fd,MIC_PCM,session,&txseq,mic_packet,mic_used*2)) { audio_fault(5,errno); break; }
                mic_used=0;
            }
        }
        /* Continuous local wake-word listening is bounded by the independent
         * 2s authenticated heartbeat watchdog, not a 30s recording timer.
         * Playback retains its existing 12s cap. No audio/motion overlap. */
        if((mode==2 || mode==4) && now_ms()-started>12000) { audio_fault(9,0); break; }
        if(mode==3 && now_ms()-started>20000) { audio_fault(10,0); break; }
    }
    /* Independent WSS owns microphone/amplifier lifetime. */
    if(mode==1) tars_audio_capture_stop();
    if(mode==2) (void)tars_audio_play_finish();
    if(staged) heap_caps_free(staged);
    atomic_store(&s_session,false);
    if(reserved) tars_calibration_release_audio();
    if(s_control_lost) s_control_lost();
    tars_state_set_audio_connected(false);
    tars_state_set_interaction(TARS_INTERACTION_IDLE);
}

static void audio_watchdog(void *arg) {
    (void)arg;
    while(true) {
        /* Read the last timestamp BEFORE the clock to avoid unsigned
         * underflow when the RX task refreshes it between the two reads. */
        uint32_t last=atomic_load(&s_last_rx);
        uint32_t elapsed=(uint32_t)now_ms()-last;
        if(atomic_load(&s_session) && elapsed>2000 && last==atomic_load(&s_last_rx)) {
            atomic_fetch_add(&s_watchdog_count,1);
            audio_fault(6,elapsed);
            atomic_store(&s_session,false);
        }
        vTaskDelay(pdMS_TO_TICKS(50)+1);
    }
}

static void network_task(void *arg) {
    (void)arg;
    while(true) {
        if(atomic_load(&s_wifi_up)) {
            int fd=socket(AF_INET,SOCK_STREAM,IPPROTO_IP);
            if(fd>=0) {
                int no_delay=1;
                setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&no_delay,sizeof(no_delay));
                /* Tolerate a brief Wi-Fi/desktop scheduling stall. The separate
                 * authenticated 2s watchdog still closes a lost connection. */
                struct timeval timeout={.tv_sec=1,.tv_usec=0};
                setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
                setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
                struct sockaddr_in addr={.sin_family=AF_INET,.sin_port=htons(s_config.port)};
                inet_pton(AF_INET,s_config.host,&addr.sin_addr);
                /* Non-blocking connect has a bounded 1-second wait. */
                int flags=fcntl(fd,F_GETFL,0); fcntl(fd,F_SETFL,flags|O_NONBLOCK);
                int rc=connect(fd,(struct sockaddr*)&addr,sizeof(addr));
                if(rc<0 && errno==EINPROGRESS) {
                    fd_set write; FD_ZERO(&write); FD_SET(fd,&write);
                    struct timeval wait={.tv_sec=1};
                    if(select(fd+1,NULL,&write,NULL,&wait)>0) {
                        int error=0; socklen_t size=sizeof(error);
                        rc=getsockopt(fd,SOL_SOCKET,SO_ERROR,&error,&size)==0 && !error ? 0 : -1;
                    }
                }
                fcntl(fd,F_SETFL,flags);
                if(rc==0) audio_session(fd);
                close(fd);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(2000)+1); /* reconnect stays idle; never replays audio */
    }
}

static void wifi_event(void *arg,esp_event_base_t base,int32_t id,void *data) {
    (void)arg; (void)data;
    if(base==WIFI_EVENT && id==WIFI_EVENT_STA_START) esp_wifi_connect();
    else if(base==WIFI_EVENT && id==WIFI_EVENT_STA_DISCONNECTED) {
        atomic_store(&s_wifi_up,false); atomic_store(&s_session,false);
        tars_audio_abort(); esp_wifi_connect();
    } else if(base==IP_EVENT && id==IP_EVENT_STA_GOT_IP) atomic_store(&s_wifi_up,true);
}

esp_err_t tars_wifi_audio_init(void) {
    esp_err_t err=nvs_flash_init();
    if(err!=ESP_OK) return err; /* NEVER erase NVS as an automatic recovery */
    nvs_handle_t nvs; err=nvs_open("tars_audio",NVS_READONLY,&nvs);
    if(err==ESP_ERR_NVS_NOT_FOUND) { ESP_LOGI(TAG,"not configured; Wi-Fi audio remains off"); return ESP_OK; }
    if(err!=ESP_OK) return err;
    size_t size=sizeof(s_config); err=nvs_get_blob(nvs,"profile",&s_config,&size); nvs_close(nvs);
    if(err!=ESP_OK || size!=sizeof(s_config)) return ESP_ERR_INVALID_STATE;
    /* Validate termination before strlen on persistent data. */
    if(!memchr(s_config.ssid,0,sizeof(s_config.ssid)) || !memchr(s_config.password,0,sizeof(s_config.password)) ||
       !memchr(s_config.host,0,sizeof(s_config.host)) || !memchr(s_config.token,0,sizeof(s_config.token)) ||
       !valid_config(s_config.ssid,s_config.password,s_config.host,s_config.port,s_config.token)) return ESP_ERR_INVALID_ARG;
    err=esp_netif_init(); if(err!=ESP_OK) return err;
    err=esp_event_loop_create_default(); if(err!=ESP_OK) return err;
    if(!esp_netif_create_default_wifi_sta()) return ESP_ERR_NO_MEM;
    wifi_init_config_t init=WIFI_INIT_CONFIG_DEFAULT();
    err=esp_wifi_init(&init); if(err!=ESP_OK) return err;
    esp_event_handler_register(WIFI_EVENT,ESP_EVENT_ANY_ID,wifi_event,NULL);
    esp_event_handler_register(IP_EVENT,IP_EVENT_STA_GOT_IP,wifi_event,NULL);
    wifi_config_t config={0};
    memcpy(config.sta.ssid,s_config.ssid,strlen(s_config.ssid));
    memcpy(config.sta.password,s_config.password,strlen(s_config.password));
    config.sta.threshold.authmode=WIFI_AUTH_WPA2_PSK;
    err=esp_wifi_set_storage(WIFI_STORAGE_RAM); if(err!=ESP_OK) return err;
    err=esp_wifi_set_mode(WIFI_MODE_STA); if(err!=ESP_OK) return err;
    err=esp_wifi_set_config(WIFI_IF_STA,&config); if(err!=ESP_OK) return err;
    if(xTaskCreate(audio_watchdog,"wifi_audio_wd",3072,NULL,6,NULL)!=pdPASS) return ESP_ERR_NO_MEM;
    if(xTaskCreate(network_task,"wifi_audio",8192,NULL,3,NULL)!=pdPASS) return ESP_ERR_NO_MEM;
    err=esp_wifi_start();
    if(err==ESP_OK) err=esp_wifi_set_ps(WIFI_PS_NONE); /* audio latency; battery/RF must be measured */
    return err;
}
