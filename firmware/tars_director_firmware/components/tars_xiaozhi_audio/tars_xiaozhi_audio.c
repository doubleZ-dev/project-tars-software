/* Xiaozhi v1 Opus/WebSocket framing, with negotiated TARS cached-playback extension.
 * No network I/O on the I2S owner. No per-audio-frame application ACK.
 * Local Companion performs wake/VAD; standby audio never goes to the cloud. */
#include "tars_xiaozhi_audio.h"
#include "tars_wifi_audio.h"
#include "tars_audio.h"
#include "tars_motion.h"
#include "tars_display.h"
#include "tars_state.h"
#include "esp_websocket_client.h"
#include "esp_opus_enc.h"
#include "esp_opus_dec.h"
#include "esp_heap_caps.h"
#include "esp_mac.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "cJSON.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/idf_additions.h"
#include <stdatomic.h>
#include <string.h>
#include <stdio.h>

#define PACKET_MAX 1536
#define CACHE_SAMPLES 128000
typedef struct { uint32_t epoch; uint16_t size; bool binary; uint8_t data[PACKET_MAX]; } packet_t;
static QueueHandle_t rxq,txq;
static atomic_bool connected,ready,failed,busy,txbusy;
static atomic_uint generation;
static atomic_uint diag_stage;
static atomic_int diag_error;
static atomic_int diag_sock,diag_flags;
static atomic_uint diag_fault,diag_stack;
static char diag_uri[96];
void tars_xiaozhi_audio_details(char *out,size_t capacity) {
 esp_netif_ip_info_t ip={0};
 esp_netif_t *netif=esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
 if(netif) esp_netif_get_ip_info(netif,&ip);
 snprintf(out,capacity,"{\"uri\":\"%s\",\"ip\":\"" IPSTR "\",\"gateway\":\"" IPSTR "\",\"stage\":%u,\"error\":%d,\"sock\":%d,\"flags\":%d,\"epoch\":%u,\"fault_line\":%u,\"stack_free\":%u,\"internal_free\":%u}",diag_uri,IP2STR(&ip.ip),IP2STR(&ip.gw),atomic_load(&diag_stage),atomic_load(&diag_error),atomic_load(&diag_sock),atomic_load(&diag_flags),atomic_load(&generation),atomic_load(&diag_fault),atomic_load(&diag_stack),(unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
}
void tars_xiaozhi_audio_diagnostic(uint32_t *stage,int *error) { *stage=atomic_load(&diag_stage); *error=atomic_load(&diag_error); }
static esp_websocket_client_handle_t socket_client;
static const char *TAG="tars_wss";
static char session[65];
extern const uint8_t cert_start[] asm("_binary_companion_pem_start");

static void fault_at(unsigned line) {
 if(!atomic_exchange(&failed,true)) atomic_store(&diag_fault,line);
 atomic_store(&ready,false); tars_audio_abort();
}
#define fault() fault_at(__LINE__)
static bool enqueue(QueueHandle_t q,const void *data,size_t size,bool binary) {
 if(!size || size>PACKET_MAX) { fault(); return false; }
 packet_t p={.epoch=atomic_load(&generation),.size=size,.binary=binary};
 memcpy(p.data,data,size);
 if(xQueueSend(q,&p,0)!=pdTRUE) { fault(); return false; }
 return true;
}
static void text(const char *s) { enqueue(txq,s,strlen(s),false); }
static void ack(int id,int status) {
 char s[160]; snprintf(s,sizeof(s),"{\"type\":\"tars.ack\",\"session_id\":\"%s\",\"id\":%d,\"status\":%d}",session,id,status); text(s);
}
static void event(void *arg,esp_event_base_t base,int32_t id,void *payload) {
 (void)arg; (void)base;
 static uint8_t assembled[PACKET_MAX]; static size_t used; static int opcode;
 if(id==WEBSOCKET_EVENT_CONNECTED) {
  atomic_store(&diag_stage,14);
  used=0; atomic_store(&connected,true);
  text("{\"type\":\"hello\",\"version\":1,\"transport\":\"websocket\",\"features\":{\"tars_cached_audio\":1},\"audio_params\":{\"format\":\"opus\",\"sample_rate\":16000,\"channels\":1,\"frame_duration\":60}}");
 } else if(id==WEBSOCKET_EVENT_DISCONNECTED || id==WEBSOCKET_EVENT_ERROR) {
  if(payload) {
   esp_websocket_event_data_t *d=payload;
   int error=0;
   /* ESP's event struct leaves TLS fields unset for non-transport events. */
   if(d->error_handle.error_type==WEBSOCKET_ERROR_TYPE_TCP_TRANSPORT) {
    atomic_store(&diag_sock,d->error_handle.esp_transport_sock_errno);
    atomic_store(&diag_flags,d->error_handle.esp_tls_cert_verify_flags);
    error=d->error_handle.esp_tls_stack_err;
    if(!error) error=d->error_handle.esp_tls_last_esp_err;
    if(!error) error=d->error_handle.esp_transport_sock_errno;
   }
   if(!error) error=d->error_handle.esp_ws_handshake_status_code;
   if(error) atomic_store(&diag_error,error);
  }
  atomic_store(&connected,false); fault();
 } else if(id==WEBSOCKET_EVENT_DATA) {
  esp_websocket_event_data_t *d=payload;
  if(d->op_code==0x9 || d->op_code==0xa) return;
  if(d->op_code==0x8) { fault(); return; }
  /* Our peer sends one unfragmented application message; library may chunk it. */
  if(d->payload_offset==0) { used=0; opcode=d->op_code; }
  if((opcode!=1 && opcode!=2) || d->payload_len>PACKET_MAX || d->payload_offset!=(int)used || d->data_len<0 || used+d->data_len>sizeof(assembled)) { fault(); return; }
  memcpy(assembled+used,d->data_ptr,d->data_len); used+=d->data_len;
  if(used==(size_t)d->payload_len) {
   if(!d->fin) { fault(); return; }
   enqueue(rxq,assembled,used,opcode==2); used=0;
  }
 }
}
static void tx_task(void *unused) {
 (void)unused; packet_t p;
 while(true) {
  if(xQueueReceive(txq,&p,pdMS_TO_TICKS(100))!=pdTRUE) continue;
  atomic_store(&txbusy,true);
  if(p.epoch==atomic_load(&generation) && atomic_load(&connected) && !atomic_load(&failed)) {
   int n=p.binary ? esp_websocket_client_send_bin(socket_client,(char*)p.data,p.size,pdMS_TO_TICKS(8000)) :
                    esp_websocket_client_send_text(socket_client,(char*)p.data,p.size,pdMS_TO_TICKS(8000));
   if(n!=p.size) fault();
  }
  atomic_store(&txbusy,false);
 }
}
static int number(cJSON *j,const char *key,int fallback) {
 cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key); return cJSON_IsNumber(v) ? v->valueint : fallback;
}
static const char *string(cJSON *j,const char *key) {
 cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key); return cJSON_IsString(v) ? v->valuestring : "";
}
static void io_task(void *unused) {
 (void)unused;
 atomic_store(&diag_stage,2);
 void *enc=NULL,*dec=NULL;
 esp_opus_enc_config_t ec={.sample_rate=16000,.channel=1,.bits_per_sample=16,.bitrate=24000,
  .frame_duration=ESP_OPUS_ENC_FRAME_DURATION_60_MS,.application_mode=ESP_OPUS_ENC_APPLICATION_VOIP,.complexity=0,.enable_vbr=true};
 esp_opus_dec_cfg_t dc={.sample_rate=16000,.channel=1,.frame_duration=ESP_OPUS_DEC_FRAME_DURATION_60_MS};
 ESP_LOGI(TAG,"codec init internal_free=%u psram_free=%u",(unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),(unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
 int codec_err=esp_opus_enc_open(&ec,sizeof(ec),&enc);
 atomic_store(&diag_stage,3); atomic_store(&diag_error,codec_err);
 if(codec_err) { ESP_LOGE(TAG,"Opus encoder init error=%d",codec_err); vTaskDelay(pdMS_TO_TICKS(1500)); abort(); }
 codec_err=esp_opus_dec_open(&dc,sizeof(dc),&dec);
 atomic_store(&diag_stage,4); atomic_store(&diag_error,codec_err);
 if(codec_err) { ESP_LOGE(TAG,"Opus decoder init error=%d",codec_err); vTaskDelay(pdMS_TO_TICKS(1500)); abort(); }
 int16_t *cache=heap_caps_malloc(CACHE_SAMPLES*2,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
 if(!cache) abort();
 int16_t frame[960],chunk[128]; uint8_t encoded[1275]; packet_t p;
 size_t fill=0,expected=0,received=0; bool capture=false,reserved=false,staging=false;
 uint32_t epoch=0; int volume=20,last_id=0; int64_t upload_started=0;
 while(true) {
  atomic_store(&busy,true);
  if(epoch!=atomic_load(&generation) || atomic_load(&failed)) {
   if(capture) { tars_audio_capture_stop(); }
   capture=false; staging=false; fill=0;
   if(reserved) { tars_calibration_release_audio(); }
   reserved=false;
   epoch=atomic_load(&generation); last_id=0;
  }
  if(xQueueReceive(rxq,&p,0)==pdTRUE && p.epoch==epoch && !atomic_load(&failed)) {
   if(p.binary) {
    if(!staging || p.size>1275) fault();
    else {
     esp_audio_dec_in_raw_t in={.buffer=p.data,.len=p.size};
     esp_audio_dec_out_frame_t out={.buffer=(uint8_t*)frame,.len=sizeof(frame)}; esp_audio_dec_info_t info={0};
     if(esp_opus_dec_decode(dec,&in,&out,&info)!=ESP_AUDIO_ERR_OK || out.decoded_size!=1920 || received>=expected) fault();
     else { size_t n=expected-received; if(n>960)n=960; memcpy(cache+received,frame,n*2); received+=n; }
    }
   } else {
    cJSON *j=cJSON_ParseWithLength((char*)p.data,p.size); int id=number(j,"id",0),status=0;
    const char *type=string(j,"type"),*op=string(j,"op");
    if(!strcmp(type,"hello") && !atomic_load(&ready)) {
     const char *sid=string(j,"session_id");
     cJSON *params=cJSON_GetObjectItemCaseSensitive(j,"audio_params");
     if(!*sid || strlen(sid)>64 || strspn(sid,"0123456789abcdef")!=strlen(sid) ||
        number(params,"sample_rate",0)!=16000 || number(params,"frame_duration",0)!=60 ||
        number(params,"channels",0)!=1 || strcmp(string(params,"format"),"opus") || number(j,"tars_cached_audio",0)!=1) fault();
     else { strcpy(session,sid); atomic_store(&ready,true); ESP_LOGI(TAG,"authenticated Opus session ready"); }
    } else if(!atomic_load(&ready) || strcmp(type,"tars.audio") || strcmp(string(j,"session_id"),session) || id<=last_id) fault();
    else {
     last_id=id;
     if(!strcmp(op,"capture_start") && !capture && !staging && !tars_motion_is_busy()) {
      reserved=tars_calibration_reserve_audio();
      status=reserved ? tars_audio_recover() : ESP_ERR_INVALID_STATE;
      if(!status) status=tars_audio_capture_start();
      if(!status) { esp_opus_enc_reset(enc); capture=true; fill=0; }
      else if(reserved) { tars_calibration_release_audio(); reserved=false; }
     } else if(!strcmp(op,"capture_stop")) {
      if(capture) { tars_audio_capture_stop(); }
      capture=false; fill=0;
      if(reserved && !staging) { tars_calibration_release_audio(); reserved=false; }
     } else if(!strcmp(op,"play_begin") && !capture && !staging && !tars_motion_is_busy()) {
      int samples=number(j,"samples",0); volume=number(j,"volume",20);
      if(samples<=0 || samples>CACHE_SAMPLES || volume<0 || volume>20) status=ESP_ERR_INVALID_ARG;
      else {
       reserved=tars_calibration_reserve_audio(); status=reserved?tars_audio_recover():ESP_ERR_INVALID_STATE;
       if(!status) { staging=true; expected=samples; received=0; upload_started=esp_timer_get_time(); esp_opus_dec_reset(dec); }
       else if(reserved) { tars_calibration_release_audio(); reserved=false; }
      }
     } else if(!strcmp(op,"play_commit") && staging && received==expected) {
      /* ACK only after I2S drain and amplifier shutdown. */
      status=tars_audio_play_start(volume);
      if(!status) { status=tars_audio_play_write(cache,expected); int end=tars_audio_play_finish(); if(!status)status=end; }
      staging=false; tars_calibration_release_audio(); reserved=false;
     } else if(!strcmp(op,"state")) {
      int state=number(j,"value",-1);
      if(state<0 || state>3) status=ESP_ERR_INVALID_ARG; else tars_state_set_interaction(state);
     } else if(!strcmp(op,"text")) {
      const char *role=string(j,"role"),*value=string(j,"value");
      status=tars_display_post_dialogue(role,value,strlen(value))?0:ESP_FAIL;
     } else status=ESP_ERR_INVALID_STATE;
     ack(id,status);
    }
    cJSON_Delete(j);
   }
  }
  if(capture && atomic_load(&ready) && !atomic_load(&failed)) {
   size_t n=0; int err=tars_audio_capture_read(chunk,128,&n);
   if(err) fault();
   else for(size_t i=0;i<n;i++) {
    frame[fill++]=chunk[i];
    if(fill==960) {
     esp_audio_enc_in_frame_t in={.buffer=(uint8_t*)frame,.len=sizeof(frame)};
     esp_audio_enc_out_frame_t out={.buffer=encoded,.len=sizeof(encoded)};
     int encoded_status=esp_opus_enc_process(enc,&in,&out);
     atomic_store(&diag_stack,uxTaskGetStackHighWaterMark(NULL));
     if(encoded_status!=ESP_AUDIO_ERR_OK || !out.encoded_bytes) { atomic_store(&diag_error,encoded_status); fault(); }
     else enqueue(txq,encoded,out.encoded_bytes,true);
     fill=0;
    }
   }
  }
  if(staging && esp_timer_get_time()-upload_started>20000000) fault();
  atomic_store(&busy,false);
  if(!capture) vTaskDelay(pdMS_TO_TICKS(2)+1);
 }
}
static void connection_task(void *unused) {
 (void)unused; char host[16],token[65],uri[96],headers[192]; uint8_t mac[6];
 atomic_store(&diag_stage,10);
 if(!tars_wifi_audio_endpoint(host,sizeof(host),token,sizeof(token))) { vTaskDelete(NULL); return; }
 esp_read_mac(mac,ESP_MAC_WIFI_STA);
 snprintf(uri,sizeof(uri),"wss://%s:8766/tars",host);
 strcpy(diag_uri,uri);
 snprintf(headers,sizeof(headers),"Authorization: Bearer %s\r\nDevice-Id: %02x:%02x:%02x:%02x:%02x:%02x\r\nProtocol-Version: 1\r\n",token,mac[0],mac[1],mac[2],mac[3],mac[4],mac[5]);
 esp_websocket_client_config_t cfg={.uri=uri,.headers=headers,.cert_pem=(const char*)cert_start,
  .disable_auto_reconnect=true,.network_timeout_ms=8000,.ping_interval_sec=2,.pingpong_timeout_sec=8,
  .buffer_size=2048,.task_stack=8192};
 socket_client=esp_websocket_client_init(&cfg);
 atomic_store(&diag_stage,11);
 ESP_ERROR_CHECK(esp_websocket_register_events(socket_client,WEBSOCKET_EVENT_ANY,event,NULL));
 while(true) {
  wifi_ap_record_t ap;
  esp_netif_ip_info_t ip={0};
  esp_netif_t *netif=esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
  if(netif) esp_netif_get_ip_info(netif,&ip);
  /* Association precedes DHCP. Never start TCP until the interface owns an IP. */
  if(!ip.ip.addr || esp_wifi_sta_get_ap_info(&ap)!=ESP_OK) { vTaskDelay(pdMS_TO_TICKS(1000)); continue; }
  atomic_store(&failed,false); atomic_store(&connected,false); atomic_store(&ready,false);
  atomic_store(&diag_stage,13);
  int start_error=esp_websocket_client_start(socket_client);
  if(start_error) { atomic_store(&diag_error,start_error); fault(); }
  int64_t begin=esp_timer_get_time();
  while(!atomic_load(&failed)) {
   if(!atomic_load(&ready) && esp_timer_get_time()-begin>15000000) fault();
   vTaskDelay(pdMS_TO_TICKS(100));
  }
  atomic_store(&connected,false); atomic_fetch_add(&generation,1);
  atomic_store(&diag_stage,20);
  esp_websocket_client_stop(socket_client);
  atomic_store(&diag_stage,21);
  /* I2S has its own epoch cleanup; do not wait for a continuously running task
     to happen to be between iterations. Old TX is bounded by send timeout. */
  while(atomic_load(&txbusy)) vTaskDelay(pdMS_TO_TICKS(10));
  /* Every queue item carries a generation, so old speech cannot reach a new session. */
  vTaskDelay(pdMS_TO_TICKS(2000));
 }
}
esp_err_t tars_xiaozhi_audio_init(void) {
 atomic_store(&diag_stage,1);
 /* 128 frames tolerate 7.68s transport stalls; overflow aborts instead of replaying. */
 static StaticQueue_t rxstate,txstate;
 uint8_t *rxmem=heap_caps_malloc(160*sizeof(packet_t),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
 uint8_t *txmem=heap_caps_malloc(128*sizeof(packet_t),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
 if(!rxmem || !txmem) return ESP_ERR_NO_MEM;
 rxq=xQueueCreateStatic(160,sizeof(packet_t),rxmem,&rxstate); txq=xQueueCreateStatic(128,sizeof(packet_t),txmem,&txstate);
 atomic_store(&failed,true);
 /* 60ms Opus SILK encoder has >22KiB measured nested stack usage. Give it
    a bounded 48KiB PSRAM stack, keeping internal RAM available for DMA/WiFi. */
 if(xTaskCreateWithCaps(io_task,"opus_i2s",49152,NULL,5,NULL,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT)!=pdPASS ||
    xTaskCreate(tx_task,"opus_tx",6144,NULL,4,NULL)!=pdPASS ||
    xTaskCreate(connection_task,"opus_link",6144,NULL,3,NULL)!=pdPASS) return ESP_ERR_NO_MEM;
 return ESP_OK;
}
