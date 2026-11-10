#include "tars_diagnostics.h"
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include "esp_attr.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"

#define MAGIC 0x54444731u /* versioned TDG1; best effort RTC, NOT flash */
typedef struct { uint32_t magic, sequence; tars_diag_mark_t mark; uint32_t checksum; } retained_t;
_Static_assert(sizeof(tars_diag_mark_t)==60,"RTC mark layout changed; update MAGIC/version");
_Static_assert(sizeof(retained_t)==72,"RTC record layout changed; update MAGIC/version");
static RTC_NOINIT_ATTR retained_t s_slots[2];
static retained_t s_current;
static tars_diag_mark_t s_previous;
static bool s_previous_valid;
static unsigned s_slot;
static esp_reset_reason_t s_reason;
static portMUX_TYPE s_mux=portMUX_INITIALIZER_UNLOCKED;

static uint32_t checksum(const retained_t *r) {
    const unsigned char *p=(const unsigned char*)r;
    uint32_t value=2166136261u;
    for(size_t i=0;i<offsetof(retained_t,checksum);++i) value=(value^p[i])*16777619u;
    return value;
}
static bool valid(const retained_t *r) {
    return r->magic==MAGIC && r->mark.stage<=TARS_DIAG_CANCELLED &&
           memchr(r->mark.command_id,0,sizeof(r->mark.command_id)) && r->checksum==checksum(r);
}
const char *tars_diag_stage_name(uint32_t stage) {
    static const char *names[]={"boot","arm","gait_start","target","hold","complete","cancelled"};
    return stage<=TARS_DIAG_CANCELLED ? names[stage] : "unknown";
}
int tars_diag_reset_reason(void) { return s_reason; }
const char *tars_diag_reset_name(void) {
    switch(s_reason) {
        case ESP_RST_POWERON:return "POWERON"; case ESP_RST_EXT:return "EXT";
        case ESP_RST_SW:return "SOFTWARE"; case ESP_RST_PANIC:return "PANIC";
        case ESP_RST_INT_WDT:return "INT_WDT"; case ESP_RST_TASK_WDT:return "TASK_WDT";
        case ESP_RST_WDT:return "WDT"; case ESP_RST_DEEPSLEEP:return "DEEPSLEEP";
        case ESP_RST_BROWNOUT:return "BROWNOUT"; case ESP_RST_SDIO:return "SDIO";
        case ESP_RST_USB:return "USB"; case ESP_RST_JTAG:return "JTAG";
        case ESP_RST_EFUSE:return "EFUSE"; case ESP_RST_PWR_GLITCH:return "PWR_GLITCH";
        case ESP_RST_CPU_LOCKUP:return "CPU_LOCKUP"; default:return "UNKNOWN";
    }
}
void tars_diag_mark(tars_diag_stage_t stage,int action,unsigned cycle,unsigned phase,const char *id) {
    retained_t next={0};
    next.magic=MAGIC;
    next.mark=(tars_diag_mark_t){.uptime_ms=(uint32_t)(esp_timer_get_time()/1000),
        .stage=stage,.action=action,.cycle=cycle,.phase=phase};
    snprintf(next.mark.command_id,sizeof(next.mark.command_id),"%s",id ? id : "");
    portENTER_CRITICAL(&s_mux);
    next.sequence=s_current.sequence+1;
    next.checksum=checksum(&next);
    s_slot^=1;
    s_slots[s_slot].magic=0; /* incomplete writes cannot masquerade as valid */
    __sync_synchronize();
    memcpy((unsigned char*)&s_slots[s_slot]+4,(unsigned char*)&next+4,sizeof(next)-4);
    __sync_synchronize();
    s_slots[s_slot].magic=MAGIC; /* commit last; other slot remains available */
    s_current=next;
    portEXIT_CRITICAL(&s_mux);
}
void tars_diag_init(void) {
    s_reason=esp_reset_reason();
    /* RTC retention is not guaranteed through supply loss. Never trust a stale
     * bank on a POWERON reset, even if its checksum happens to be intact. */
    bool a=s_reason!=ESP_RST_POWERON && valid(&s_slots[0]);
    bool b=s_reason!=ESP_RST_POWERON && valid(&s_slots[1]);
    s_previous_valid=a||b;
    if(s_previous_valid) {
        s_slot=b && (!a || (int32_t)(s_slots[1].sequence-s_slots[0].sequence)>0) ? 1 : 0;
        s_current=s_slots[s_slot]; s_previous=s_current.mark;
    } else {
        memset(s_slots,0,sizeof(s_slots)); memset(&s_current,0,sizeof(s_current)); s_slot=0;
    }
    tars_diag_mark(TARS_DIAG_BOOT,-1,0,0,NULL);
}
bool tars_diag_get(bool previous,tars_diag_mark_t *out) {
    if(!out) return false;
    portENTER_CRITICAL(&s_mux);
    bool ok=!previous || s_previous_valid;
    *out=previous ? s_previous : s_current.mark;
    portEXIT_CRITICAL(&s_mux);
    return ok;
}
void tars_diag_log_boot(void) {
    ESP_LOGW("tars_diag","reset=%s(%d) previous_valid=%d; RTC is best effort, no voltage measurement",
             tars_diag_reset_name(),s_reason,s_previous_valid);
    if(s_previous_valid) ESP_LOGW("tars_diag","previous stage=%s action=%ld cycle=%lu phase=%lu at_ms=%lu command=%s",
        tars_diag_stage_name(s_previous.stage),(long)s_previous.action,
        (unsigned long)s_previous.cycle,(unsigned long)s_previous.phase,
        (unsigned long)s_previous.uptime_ms,s_previous.command_id);
}
