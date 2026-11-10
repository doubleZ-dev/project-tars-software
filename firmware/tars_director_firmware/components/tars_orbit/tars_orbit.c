#include "tars_orbit.h"
#include "reference_pixel.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include <assert.h>
#include <stdatomic.h>

typedef enum { CMD_STATE, CMD_ACTION, CMD_CANCEL, CMD_VOICE, CMD_GALLERY, CMD_DIRECT, CMD_RELEASE } command_kind_t;
typedef struct { command_kind_t kind; int id; uint32_t value, ticket; } command_t;
typedef struct {
    lv_obj_t *surface, *caption;
    orbit_pose_t pose, from;
    orbit_frame_t frame;
} screen_t;
static screen_t s_screens[2];
static QueueHandle_t s_commands;
static orbit_control_t s_control;
static bool s_gallery;
static uint32_t s_gallery_started, s_transition, s_voice_updated;
static unsigned s_voice;
static int s_previous=-1;
static int s_manual_id=-1;
static uint32_t s_manual_started, s_manual_duration;
static atomic_uint s_ticket, s_applied;
static atomic_int s_current;
static atomic_bool s_manual;
static atomic_uint s_latest_voice;
static int s_deferred_state=-1;
static atomic_bool s_visible=true;
void tars_orbit_set_visible(bool visible) { atomic_store(&s_visible,visible); }

uint32_t tars_orbit_direct(const char *name, uint32_t duration_ms) {
    int id=orbit_find(name);
    if(id<0 || duration_ms>3600000 || !s_commands) return 0;
    uint32_t ticket=atomic_fetch_add(&s_ticket,1)+1;
    command_t c={CMD_DIRECT,id,duration_ms,ticket};
    return xQueueSend(s_commands,&c,0)==pdTRUE ? ticket : 0;
}
uint32_t tars_orbit_release(void) {
    if(!s_commands) return 0;
    uint32_t ticket=atomic_fetch_add(&s_ticket,1)+1;
    command_t c={CMD_RELEASE,0,0,ticket};
    return xQueueSend(s_commands,&c,0)==pdTRUE ? ticket : 0;
}
void tars_orbit_status(uint32_t *applied,int *current,bool *manual) {
    *applied=atomic_load(&s_applied);*current=atomic_load(&s_current);*manual=atomic_load(&s_manual);
}

static bool post(command_kind_t kind,int id,uint32_t value) {
    command_t c={kind,id,value,0};return s_commands && xQueueSend(s_commands,&c,0)==pdTRUE;
}
bool tars_orbit_set_state(orbit_id_t state) {
    return state>=0 && state<ORBIT_COUNT && !orbit_is_action(state) && post(CMD_STATE,state,0);
}
bool tars_orbit_play_action(const char *name,uint32_t duration) {
    int id=orbit_find(name);return id>=0 && orbit_is_action(id) && post(CMD_ACTION,id,duration);
}
bool tars_orbit_cancel_action(void) { return post(CMD_CANCEL,0,0); }
bool tars_orbit_set_voice_level(uint8_t level) { atomic_store(&s_latest_voice,256+level);return true; }
bool tars_orbit_set_gallery(bool enabled) { return post(CMD_GALLERY,0,enabled); }

static void paint(lv_event_t *event) {
    screen_t *s=lv_event_get_user_data(event);
    lv_layer_t *layer=lv_event_get_layer(event);
    lv_area_t bounds;lv_obj_get_coords(s->surface,&bounds);
    for(unsigned i=0;i<s->frame.count;i++) {
        const orbit_primitive_t *p=&s->frame.items[i];
        if(p->kind==0){
            lv_draw_rect_dsc_t d;lv_draw_rect_dsc_init(&d);
            d.bg_color=lv_color_hex(p->color);d.bg_opa=p->opacity;d.radius=p->radius;
            lv_area_t a={bounds.x1+p->x,bounds.y1+p->y,bounds.x1+p->x+p->a-1,bounds.y1+p->y+p->b-1};
            lv_draw_rect(layer,&d,&a);
        }else{
            lv_draw_line_dsc_t d;lv_draw_line_dsc_init(&d);
            d.p1=(lv_point_precise_t){bounds.x1+p->x,bounds.y1+p->y};
            d.p2=(lv_point_precise_t){bounds.x1+p->a,bounds.y1+p->b};
            d.width=p->width;d.color=lv_color_hex(p->color);d.opa=p->opacity;
            d.round_start=1;d.round_end=1;lv_draw_line(layer,&d);
        }
    }
}
static void timer_cb(lv_timer_t *timer) {
    (void)timer;uint32_t now=lv_tick_get(),elapsed=0,duration=0;
    /* Resolve boot expiration before handling an action arriving just after startup. */
    (void)orbit_control_current(&s_control,now,&elapsed,&duration);
    if(s_control.state!=ORBIT_BOOT && s_deferred_state>=0){
        orbit_control_state(&s_control,s_deferred_state,now);s_deferred_state=-1;
    }
    command_t c;
    uint32_t applied=atomic_load(&s_applied);
    while(xQueueReceive(s_commands,&c,0)==pdTRUE){
        switch(c.kind){
        case CMD_STATE:
            s_gallery=false;
            if(s_control.state==ORBIT_BOOT && c.id!=ORBIT_ERROR) s_deferred_state=c.id;
            else { orbit_control_state(&s_control,c.id,now);s_deferred_state=-1; }
            break;
        case CMD_ACTION:
            s_gallery=false;
            if(!orbit_control_action(&s_control,c.id,c.value,now))
                ESP_LOGI("orbit","action suppressed by %s",orbit_name(s_control.state));
            else s_previous=-1; /* Explicit same-action replay restarts transition. */
            break;
        case CMD_CANCEL: s_gallery=false;orbit_control_cancel(&s_control);break;
        case CMD_VOICE: s_voice=c.value;s_voice_updated=now;break;
        case CMD_GALLERY:
            s_gallery=c.value!=0;s_gallery_started=now;s_previous=-1;
            orbit_control_init(&s_control,now);break;
        case CMD_DIRECT:
            s_gallery=false;s_manual_id=c.id;s_manual_started=now;
            s_manual_duration=c.value;s_previous=-1;applied=c.ticket;break;
        case CMD_RELEASE:
            s_manual_id=-1;s_gallery=false;orbit_control_cancel(&s_control);applied=c.ticket;break;
        }
    }
    orbit_id_t id=orbit_control_current(&s_control,now,&elapsed,&duration);
    if(s_manual_id>=0 && s_manual_duration && now-s_manual_started>=s_manual_duration) s_manual_id=-1;
    if(s_manual_id>=0 && s_control.state!=ORBIT_ERROR && s_control.state!=ORBIT_BOOT) {
        id=s_manual_id;duration=orbit_duration(id);elapsed=now-s_manual_started;
        if(duration) elapsed%=duration;
    }
    unsigned latest=atomic_exchange(&s_latest_voice,0);
    if(latest){s_voice=latest-256;s_voice_updated=now;}
    unsigned voice=now-s_voice_updated<350?s_voice:0;
    if(s_gallery){
        uint32_t age=now-s_gallery_started;
        uint32_t boot_ms=orbit_duration(ORBIT_BOOT);
        if(age<boot_ms){id=ORBIT_BOOT;elapsed=age;duration=boot_ms;}
        else{
            const uint32_t cycle=(ORBIT_COUNT-1)*5000U;
            if(age>=boot_ms+cycle){s_gallery_started+=((age-boot_ms)/cycle)*cycle;age=now-s_gallery_started;}
            uint32_t phase=(age-boot_ms)%5000;
            id=1+(age-boot_ms)/5000;
            duration=orbit_duration(id);
            elapsed=duration?(uint32_t)((uint64_t)phase*duration/5000):phase;
        }
        /* Gallery-only synthetic envelope, never represented as live audio metering. */
        if(id==ORBIT_SPEAKING)voice=60+(elapsed/120%5)*35;
    }
    if((int)id!=s_previous){
        for(unsigned i=0;i<2;i++){
            s_screens[i].from=s_screens[i].pose;
            lv_label_set_text_fmt(s_screens[i].caption,"%02d %s",id,orbit_name(id));
            if(s_gallery)lv_obj_remove_flag(s_screens[i].caption,LV_OBJ_FLAG_HIDDEN);
            else lv_obj_add_flag(s_screens[i].caption,LV_OBJ_FLAG_HIDDEN);
        }
        s_transition=now;s_previous=id;ESP_LOGI("orbit","expression=%s mode=%s",orbit_name(id),s_gallery?"gallery":"events");
    }
    unsigned q=(now-s_transition)>=200?256:(now-s_transition)*256/200;
    for(unsigned i=0;i<2;i++){
        if(atomic_load(&s_visible)) lv_obj_remove_flag(s_screens[i].surface,LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(s_screens[i].surface,LV_OBJ_FLAG_HIDDEN);
        if(s_gallery)lv_obj_remove_flag(s_screens[i].caption,LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(s_screens[i].caption,LV_OBJ_FLAG_HIDDEN);
        orbit_pose_t target;orbit_sample(id,i,elapsed,duration,voice,&target);
        orbit_blend(&s_screens[i].from,&target,id==ORBIT_BOOT?256:q,&s_screens[i].pose);
        #if CONFIG_TARS_REFERENCE_PIXEL
        pixel_frame(id,i,elapsed,duration,voice,&s_screens[i].frame);
#else
        orbit_frame(&s_screens[i].pose,i,elapsed,&s_screens[i].frame);
#endif
        lv_obj_invalidate(s_screens[i].surface);
    }
    atomic_store(&s_current,id);atomic_store(&s_manual,s_manual_id>=0);
    atomic_store(&s_applied,applied);
}
void tars_orbit_init(lv_obj_t *cs18_screen,lv_obj_t *cs6_screen,bool gallery) {
    assert(!s_commands);s_commands=xQueueCreate(16,sizeof(command_t));assert(s_commands);
    lv_obj_t *parents[]={cs18_screen,cs6_screen};
    for(unsigned i=0;i<2;i++){
        screen_t *s=&s_screens[i];
        lv_obj_set_style_bg_color(parents[i],lv_color_black(),0);
        lv_obj_set_style_bg_opa(parents[i],LV_OPA_COVER,0);
        s->surface=lv_obj_create(parents[i]);lv_obj_remove_style_all(s->surface);
        lv_obj_set_pos(s->surface,0,54);lv_obj_set_size(s->surface,128,106);
        lv_obj_set_style_bg_color(s->surface,lv_color_black(),0);
        lv_obj_set_style_bg_opa(s->surface,LV_OPA_COVER,0);
        lv_obj_remove_flag(s->surface,LV_OBJ_FLAG_SCROLLABLE|LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(s->surface,paint,LV_EVENT_DRAW_MAIN,s);
        s->caption=lv_label_create(s->surface);lv_obj_set_pos(s->caption,3,96);
        lv_obj_set_width(s->caption,122);lv_label_set_long_mode(s->caption,LV_LABEL_LONG_CLIP);
        lv_obj_set_style_text_font(s->caption,&lv_font_montserrat_8,0);
        lv_obj_set_style_text_color(s->caption,lv_color_hex(0x6F9D98),0);
        orbit_sample(ORBIT_BOOT,i,0,3000,0,&s->pose);
    }
    s_gallery=gallery;s_gallery_started=lv_tick_get();
    orbit_control_init(&s_control,s_gallery_started);
    timer_cb(NULL);assert(lv_timer_create(timer_cb,40,NULL));
}
