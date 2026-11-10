#include "tars_protocol.h"

#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include "cJSON.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "tars_audio.h"
#include "tars_wifi_audio.h"
#include "tars_xiaozhi_audio.h"
#include "tars_board.h"
#include "tars_usb.h"
#include "tars_display.h"
#include "tars_orbit.h"
#include "tars_motion.h"
#include "tars_emotion.h"
#include "tars_state.h"
#include "tars_diagnostics.h"

#define CACHE_SIZE 32
#define RESPONSE_MAX 512
#define TARS_FIRMWARE_VERSION "0.18.0-director"
#define TARS_RELEASE_ID "tars-director-v1"

typedef struct {
    bool used;
    bool terminal;
    char command_id[TARS_COMMAND_ID_MAX];
    uint16_t request_length;
    char request[TARS_PROTOCOL_MAX_LINE + 1];
    char last_response[RESPONSE_MAX];
} command_cache_entry_t;

static command_cache_entry_t s_cache[CACHE_SIZE];
static unsigned s_cache_cursor;
static SemaphoreHandle_t s_cache_lock;
static SemaphoreHandle_t s_tx_lock;
static SemaphoreHandle_t s_handler_lock;
static atomic_bool s_owner_wifi;
static bool s_input_wifi; /* accessed only while holding s_handler_lock */
static char s_boot_id[24];
static char s_session_id[TARS_COMMAND_ID_MAX];
static bool s_session_active;
static int64_t s_last_rx_ms;
static atomic_bool s_audio_job_active;
typedef struct {
    char command_id[TARS_COMMAND_ID_MAX];
    char session_id[TARS_COMMAND_ID_MAX];
    tars_audio_self_test_config_t config;
} audio_job_t;
static audio_job_t s_audio_job;

static void send_hello(void);

static void link_lost(void) {
    tars_orbit_release();
    if(atomic_load(&s_owner_wifi)) tars_wifi_audio_drop_session();
    if(!tars_wifi_audio_independent()) tars_audio_abort();
    s_session_active = false;
    tars_motion_disarm();
    tars_state_set_connected(false);
    tars_usb_begin_session();
}

static void send_raw(const char *json) {
    if (!json) return;
    xSemaphoreTake(s_tx_lock, portMAX_DELAY);
    bool sent=atomic_load(&s_owner_wifi) ? tars_wifi_audio_send_control(json) : tars_usb_send_protocol(json);
    if (!sent) tars_motion_disarm();
    xSemaphoreGive(s_tx_lock);
}

static void cache_response(const char *command_id, const char *response) {
    if (!command_id || !response || !s_cache_lock) return;
    xSemaphoreTake(s_cache_lock, portMAX_DELAY);
    for (int i = 0; i < CACHE_SIZE; ++i) {
        if (s_cache[i].used && strcmp(s_cache[i].command_id, command_id) == 0) {
            snprintf(s_cache[i].last_response, sizeof(s_cache[i].last_response), "%s", response);
            break;
        }
    }
    xSemaphoreGive(s_cache_lock);
}

static void cache_mark_terminal(const char *command_id) {
    if (!command_id) return;
    xSemaphoreTake(s_cache_lock, portMAX_DELAY);
    for (int i = 0; i < CACHE_SIZE; ++i) {
        if (s_cache[i].used && strcmp(s_cache[i].command_id, command_id) == 0) {
            s_cache[i].terminal = true;
            break;
        }
    }
    xSemaphoreGive(s_cache_lock);
}

static void send_event(const char *type, const char *command_id, const char *state,
                       const char *error_code, bool retryable, cJSON *payload) {
    cJSON *root = cJSON_CreateObject();
    cJSON_AddNumberToObject(root, "v", 2);
    cJSON_AddStringToObject(root, "type", type);
    cJSON_AddStringToObject(root, "boot_id", s_boot_id);
    cJSON_AddStringToObject(root, "session_id", s_session_id);
    if (command_id) cJSON_AddStringToObject(root, "command_id", command_id);
    cJSON_AddNumberToObject(root, "ts_ms", esp_timer_get_time() / 1000);
    if (state) cJSON_AddStringToObject(root, "state", state);
    if (error_code) {
        cJSON *error = cJSON_AddObjectToObject(root, "error");
        cJSON_AddStringToObject(error, "code", error_code);
        cJSON_AddBoolToObject(error, "retryable", retryable);
    }
    if (payload) cJSON_AddItemToObject(root, "payload", payload);
    char buffer[RESPONSE_MAX];
    if (cJSON_PrintPreallocated(root, buffer, sizeof(buffer), false)) {
        send_raw(buffer);
        if (!error_code || strcmp(error_code, "command_id_conflict") != 0) {
            cache_response(command_id, buffer);
            if ((state && strcmp(state, "rejected") == 0) ||
                strcmp(type, "command.completed") == 0 || strcmp(type, "command.failed") == 0 ||
                strcmp(type, "command.cancelled") == 0) cache_mark_terminal(command_id);
        }
    }
    cJSON_Delete(root);
}

static bool valid_command_id(const cJSON *item) {
    if (!cJSON_IsString(item) || !item->valuestring) return false;
    size_t n = strlen(item->valuestring);
    if (n < 1 || n >= TARS_COMMAND_ID_MAX) return false;
    for (size_t i = 0; i < n; ++i) {
        char c = item->valuestring[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-')) return false;
    }
    return true;
}

static int cache_lookup_or_add(const char *command_id, const char *request, size_t request_length,
                               char replay[RESPONSE_MAX]) {
    replay[0] = '\0';
    xSemaphoreTake(s_cache_lock, portMAX_DELAY);
    for (int i = 0; i < CACHE_SIZE; ++i) {
        if (!s_cache[i].used || strcmp(s_cache[i].command_id, command_id) != 0) continue;
        if (s_cache[i].request_length != request_length || memcmp(s_cache[i].request, request, request_length) != 0) {
            xSemaphoreGive(s_cache_lock); return -1;
        }
        snprintf(replay, RESPONSE_MAX, "%s", s_cache[i].last_response);
        xSemaphoreGive(s_cache_lock);
        return 1;
    }
    command_cache_entry_t *entry = NULL;
    for (int n = 0; n < CACHE_SIZE; ++n) {
        command_cache_entry_t *candidate = &s_cache[s_cache_cursor++ % CACHE_SIZE];
        if (!candidate->used || candidate->terminal) { entry = candidate; break; }
    }
    if (!entry) { xSemaphoreGive(s_cache_lock); return -2; }
    *entry = (command_cache_entry_t){.used = true, .request_length = (uint16_t)request_length};
    snprintf(entry->command_id, sizeof(entry->command_id), "%s", command_id);
    memcpy(entry->request, request, request_length);
    entry->request[request_length] = '\0';
    xSemaphoreGive(s_cache_lock);
    return 0;
}

static bool json_int_range(cJSON *object, const char *name, int min, int max, int *out) {
    cJSON *item = cJSON_GetObjectItemCaseSensitive(object, name);
    if (!cJSON_IsNumber(item) || item->valuedouble != (double)item->valueint || item->valueint < min || item->valueint > max) return false;
    *out = item->valueint;
    return true;
}

static bool valid_utf8(const char *text, size_t length, bool reject_controls) {
    const unsigned char *s = (const unsigned char *)text;
    size_t i = 0;
    while (i < length) {
        uint32_t cp;
        unsigned char c = s[i++];
        if (c <= 0x7f) {
            cp = c;
        } else if (c >= 0xc2 && c <= 0xdf) {
            if (i >= length || (s[i] & 0xc0) != 0x80) return false;
            cp = ((uint32_t)(c & 0x1f) << 6) | (s[i++] & 0x3f);
        } else if (c >= 0xe0 && c <= 0xef) {
            if (i + 1 >= length || (s[i] & 0xc0) != 0x80 || (s[i + 1] & 0xc0) != 0x80) return false;
            if ((c == 0xe0 && s[i] < 0xa0) || (c == 0xed && s[i] >= 0xa0)) return false;
            cp = ((uint32_t)(c & 0x0f) << 12) | ((uint32_t)(s[i] & 0x3f) << 6) | (s[i + 1] & 0x3f);
            i += 2;
        } else if (c >= 0xf0 && c <= 0xf4) {
            if (i + 2 >= length || (s[i] & 0xc0) != 0x80 ||
                (s[i + 1] & 0xc0) != 0x80 || (s[i + 2] & 0xc0) != 0x80) return false;
            if ((c == 0xf0 && s[i] < 0x90) || (c == 0xf4 && s[i] >= 0x90)) return false;
            cp = ((uint32_t)(c & 0x07) << 18) | ((uint32_t)(s[i] & 0x3f) << 12) |
                 ((uint32_t)(s[i + 1] & 0x3f) << 6) | (s[i + 2] & 0x3f);
            i += 3;
        } else {
            return false;
        }
        if (reject_controls && (cp <= 0x1f || (cp >= 0x7f && cp <= 0x9f))) return false;
    }
    return true;
}

static void handle_dialogue(const char *command_id, cJSON *payload) {
    cJSON *role = cJSON_GetObjectItemCaseSensitive(payload, "role");
    cJSON *text = cJSON_GetObjectItemCaseSensitive(payload, "text");
    if (!cJSON_IsString(role) || !role->valuestring ||
        (strcmp(role->valuestring, "user") != 0 && strcmp(role->valuestring, "tars") != 0) ||
        !cJSON_IsString(text) || !text->valuestring) {
        send_event("command.ack", command_id, "rejected", "invalid_payload", false, NULL);
        return;
    }
    size_t text_length = strlen(text->valuestring);
    if (text_length == 0) {
        send_event("command.ack", command_id, "rejected", "invalid_payload", false, NULL);
        return;
    }
    if (text_length > TARS_DIALOGUE_TEXT_MAX_BYTES) {
        send_event("command.ack", command_id, "rejected", "dialogue_too_long", false, NULL);
        return;
    }
    if (!valid_utf8(text->valuestring, text_length, true)) {
        send_event("command.ack", command_id, "rejected", "invalid_dialogue_text", false, NULL);
        return;
    }
    if (!tars_display_post_dialogue(role->valuestring, text->valuestring, text_length)) {
        send_event("command.ack", command_id, "rejected", "display_unavailable", true, NULL);
        return;
    }
    send_event("command.ack", command_id, "accepted", NULL, false, NULL);
    send_event("command.completed", command_id, "completed", NULL, false, NULL);
}

static void motion_event(const char *command_id, tars_motion_event_t event) {
    if (event == TARS_MOTION_EVENT_COMPLETED)
        send_event("command.completed", command_id, "completed", NULL, false, NULL);
    else
        send_event("command.cancelled", command_id, "cancelled", NULL, false, NULL);
}

static bool test_action(cJSON *payload,tars_action_t *action) {
    cJSON *name=cJSON_GetObjectItemCaseSensitive(payload,"action");
    if(!name) { *action=TARS_ACTION_FORWARD;return true; }
    if(!cJSON_IsString(name)) return false;
    if(!strcmp(name->valuestring,"walk_forward")) *action=TARS_ACTION_FORWARD;
    else if(!strcmp(name->valuestring,"turn_left")) *action=TARS_ACTION_LEFT;
    else if(!strcmp(name->valuestring,"turn_right")) *action=TARS_ACTION_RIGHT;
    else return false;
    return true;
}
static void handle_test_plan(const char *id,cJSON *payload) {
    tars_action_t action;
    cJSON *view=cJSON_GetObjectItemCaseSensitive(payload,"view");
    if(!test_action(payload,&action) || !cJSON_IsString(view) || (strcmp(view->valuestring,"frames") && strcmp(view->valuestring,"calibration"))) {
        send_event("command.ack",id,"rejected","invalid_payload",false,NULL); return;
    }
    cJSON *p=cJSON_CreateObject(),*r=cJSON_AddObjectToObject(p,"result");
    cJSON_AddNumberToObject(r,"hold_ms",TARS_GAIT_HOLD_MS);
    cJSON_AddNumberToObject(r,"max_phases",TARS_TEST_MAX_PHASES);
    cJSON_AddBoolToObject(r,"drop_ramp",true);
    cJSON_AddBoolToObject(r,"free_angles",true);
    cJSON_AddBoolToObject(r,"turn_smooth",true);
    if(!strcmp(view->valuestring,"frames")) {
        int16_t frames[TARS_GAIT_PHASES][TARS_SERVO_COUNT]; tars_motion_gait_plan(action,frames);
        cJSON *array=cJSON_AddArrayToObject(r,"frames");
        for(int f=0;f<TARS_GAIT_PHASES;++f) {
            cJSON *row=cJSON_CreateArray(); cJSON_AddItemToArray(array,row);
            for(int j=0;j<TARS_SERVO_COUNT;++j) cJSON_AddItemToArray(row,cJSON_CreateNumber(frames[f][j]));
        }
    } else {
        cJSON *zeros=cJSON_AddArrayToObject(r,"zeros"),*dirs=cJSON_AddArrayToObject(r,"directions");
        cJSON *limits=cJSON_AddArrayToObject(r,"limits");
        cJSON *extra=cJSON_AddArrayToObject(r,"extra_limits");
        for(int j=0;j<TARS_SERVO_COUNT;++j) {
            const tars_servo_calibration_t *c=&TARS_SERVO_CALIBRATION[j];
            cJSON_AddItemToArray(zeros,cJSON_CreateNumber(c->zero_deg));
            cJSON_AddItemToArray(dirs,cJSON_CreateNumber(c->direction));
            int range[]={c->physical_min_deg,c->physical_max_deg};
            cJSON_AddItemToArray(limits,cJSON_CreateIntArray(range,2));
            tars_motion_test_extra_limits(action,j,&range[0],&range[1]);
            cJSON_AddItemToArray(extra,cJSON_CreateIntArray(range,2));
        }
    }
    send_event("command.ack",id,"accepted",NULL,false,NULL);
    send_event("command.completed",id,"completed",NULL,false,p);
}
static void handle_test_run(const char *id,cJSON *payload) {
    tars_action_t action;
    cJSON *frames=cJSON_GetObjectItemCaseSensitive(payload,"frames");
    cJSON *confirm=cJSON_GetObjectItemCaseSensitive(payload,"confirm");
    int count=cJSON_IsArray(frames) ? cJSON_GetArraySize(frames) : 0;
    if(!test_action(payload,&action) || !cJSON_IsTrue(confirm) || count<1 || count>TARS_TEST_MAX_PHASES) {
        send_event("command.ack",id,"rejected","invalid_test_plan",false,NULL); return;
    }
    tars_motion_command_t cmd={.action=action,.repeat=1,.frame_ms=TARS_GAIT_HOLD_MS,
                               .test_count=count};
    cJSON *ramp=cJSON_GetObjectItemCaseSensitive(payload,"drop_ramp");
    if(ramp && !cJSON_IsBool(ramp)) goto invalid;
    cmd.test_drop_ramp=cJSON_IsTrue(ramp);
    cJSON *smooth=cJSON_GetObjectItemCaseSensitive(payload,"smooth");
    cJSON *settle=cJSON_GetObjectItemCaseSensitive(payload,"settle");
    if(smooth && (!cJSON_IsNumber(smooth) || smooth->valuedouble!=smooth->valueint || smooth->valueint<0 || smooth->valueint>62)) goto invalid;
    if(settle && (!cJSON_IsNumber(settle) || (settle->valuedouble!=0 && settle->valuedouble!=60))) goto invalid;
    cmd.test_smooth_mask=smooth ? smooth->valueint : 0;
    cmd.test_settle_ms=settle ? settle->valueint : 0;
    snprintf(cmd.command_id,sizeof(cmd.command_id),"%s",id);
    for(int f=0;f<count;++f) {
        cJSON *row=cJSON_GetArrayItem(frames,f);
        if(!cJSON_IsArray(row) || cJSON_GetArraySize(row)!=TARS_SERVO_COUNT) goto invalid;
        for(int j=0;j<TARS_SERVO_COUNT;++j) {
            cJSON *n=cJSON_GetArrayItem(row,j);
            if(!cJSON_IsNumber(n) || n->valuedouble!=n->valueint || n->valueint < -45 || n->valueint > 45) goto invalid;
            cmd.test_frames[f][j]=n->valueint;
        }
    }
    if(!tars_motion_test_valid(&cmd)) goto invalid;
    tars_motion_result_t result=tars_motion_submit(&cmd);
    if(result==TARS_MOTION_OK) send_event("command.ack",id,"accepted",NULL,false,NULL);
    else send_event("command.ack",id,"rejected",
        result==TARS_MOTION_DISARMED ? "disarmed" : result==TARS_MOTION_ESTOP ? "estop_latched" : "busy_or_unsafe",false,NULL);
    return;
invalid:
    send_event("command.ack",id,"rejected","invalid_test_plan",false,NULL);
}

static struct {
    char id[40]; int64_t expires_ms; uint32_t received;
    tars_motion_command_t command;
} s_emotion_upload;

static void handle_emotion_edit(const char *type,const char *id,cJSON *payload) {
    if(!strcmp(type,"motion.test_emotion_caps")) {
        cJSON *p=cJSON_CreateObject(),*r=cJSON_AddObjectToObject(p,"result");
        cJSON_AddNumberToObject(r,"schema",2);
        cJSON_AddNumberToObject(r,"max_frames",TARS_EMOTION_MAX_FRAMES);
        cJSON_AddNumberToObject(r,"max_total_ms",30000);
        cJSON_AddNumberToObject(r,"side_limit",20);
        cJSON_AddNumberToObject(r,"lift_limit",30);
        cJSON_AddNumberToObject(r,"move_min_ms",200);
        cJSON_AddNumberToObject(r,"move_max_ms",3000);
        cJSON_AddNumberToObject(r,"hold_max_ms",3000);
        cJSON_AddNumberToObject(r,"tick_ms",10);
        send_event("command.ack",id,"accepted",NULL,false,NULL);
        send_event("command.completed",id,"completed",NULL,false,p); return;
    }
    cJSON *plan=cJSON_GetObjectItemCaseSensitive(payload,"plan_id");
    if(!valid_command_id(plan) || tars_motion_is_busy()) goto invalid_edit;
    int64_t now=esp_timer_get_time()/1000;
    if(!strcmp(type,"motion.test_emotion_begin")) {
        int count;
        if(tars_motion_is_armed() || tars_calibration_status().active ||
           !json_int_range(payload,"count",1,TARS_EMOTION_MAX_FRAMES,&count)) goto invalid_edit;
        memset(&s_emotion_upload,0,sizeof(s_emotion_upload));
        snprintf(s_emotion_upload.id,sizeof(s_emotion_upload.id),"%s",plan->valuestring);
        s_emotion_upload.expires_ms=now+30000;
        s_emotion_upload.command.emotion_id=255;
        s_emotion_upload.command.emotion_count=count;
        s_emotion_upload.command.repeat=1;
    } else {
        if(!s_emotion_upload.id[0] || now>s_emotion_upload.expires_ms ||
           strcmp(plan->valuestring,s_emotion_upload.id)) goto invalid_edit;
        if(!strcmp(type,"motion.test_emotion_frame")) {
            int index;
            cJSON *row=cJSON_GetObjectItemCaseSensitive(payload,"frame");
            if(tars_motion_is_armed() || !json_int_range(payload,"index",0,s_emotion_upload.command.emotion_count-1,&index) ||
               !cJSON_IsArray(row) || cJSON_GetArraySize(row)!=6) goto invalid_edit;
            tars_emotion_frame_t frame={0};
            for(int j=0;j<6;++j) {
                cJSON *n=cJSON_GetArrayItem(row,j);
                int lo=j<4 ? ((j==1 || j==2)?-30:-20) : j==4 ? 200:0;
                int hi=j<4 ? ((j==1 || j==2)?30:20) : 3000;
                if(!cJSON_IsNumber(n) || n->valuedouble!=n->valueint || n->valueint<lo || n->valueint>hi ||
                   (j>=4 && n->valueint%10)) goto invalid_edit;
                if(j<4) frame.logical[j]=n->valueint;
                else if(j==4) frame.move_ms=n->valueint;
                else frame.hold_ms=n->valueint;
            }
            if(frame.logical[1]!=frame.logical[2]) goto invalid_edit;
            s_emotion_upload.command.emotion_frames[index]=frame;
            s_emotion_upload.received|=1u<<index;
        } else if(!strcmp(type,"motion.test_emotion_custom")) {
            cJSON *support=cJSON_GetObjectItemCaseSensitive(payload,"support_confirm");
            if(!cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(payload,"confirm")) ||
               (support && !cJSON_IsBool(support)) ||
               s_emotion_upload.received!=((1u<<s_emotion_upload.command.emotion_count)-1)) goto invalid_edit;
            tars_motion_command_t command=s_emotion_upload.command;
            command.emotion_support_confirm=cJSON_IsTrue(support);
            snprintf(command.command_id,sizeof(command.command_id),"%s",id);
            if(!tars_motion_emotion_valid(&command)) goto invalid_edit;
            memset(&s_emotion_upload,0,sizeof(s_emotion_upload));
            tars_motion_result_t result=tars_motion_submit(&command);
            if(result==TARS_MOTION_OK) send_event("command.ack",id,"accepted",NULL,false,NULL);
            else send_event("command.ack",id,"rejected",result==TARS_MOTION_DISARMED?"disarmed":"busy_or_unsafe",false,NULL);
            return;
        } else goto invalid_edit;
    }
    send_event("command.ack",id,"accepted",NULL,false,NULL);
    send_event("command.completed",id,"completed",NULL,false,NULL); return;
invalid_edit:
    memset(&s_emotion_upload,0,sizeof(s_emotion_upload));
    send_event("command.ack",id,"rejected","invalid_emotion_upload",false,NULL);
}

static void handle_emotion_plan(const char *id) {
    cJSON *p=cJSON_CreateObject(),*r=cJSON_AddObjectToObject(p,"result");
    cJSON_AddStringToObject(r,"hash",tars_emotion_hash());
    cJSON_AddNumberToObject(r,"count",tars_emotion_count());
    send_event("command.ack",id,"accepted",NULL,false,NULL);
    send_event("command.completed",id,"completed",NULL,false,p);
}
static void handle_emotion_run(const char *id,cJSON *payload) {
    cJSON *name=cJSON_GetObjectItemCaseSensitive(payload,"name");
    cJSON *hash=cJSON_GetObjectItemCaseSensitive(payload,"hash");
    cJSON *support=cJSON_GetObjectItemCaseSensitive(payload,"support_confirm");
    unsigned emotion=cJSON_IsString(name) ? tars_emotion_find(name->valuestring) : 0;
    if(!emotion || !cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(payload,"confirm")) ||
       !cJSON_IsString(hash) || strcmp(hash->valuestring,tars_emotion_hash()) ||
       (support && !cJSON_IsBool(support))) {
        send_event("command.ack",id,"rejected","invalid_emotion_plan",false,NULL); return;
    }
    const tars_emotion_plan_t *p=tars_emotion_get(emotion);
    if(p->requires_support && !cJSON_IsTrue(support)) {
        send_event("command.ack",id,"rejected","support_confirmation_required",false,NULL); return;
    }
    tars_motion_command_t cmd={.action=TARS_ACTION_FORWARD,.repeat=1,
        .emotion_id=emotion,.emotion_support_confirm=cJSON_IsTrue(support)};
    snprintf(cmd.command_id,sizeof(cmd.command_id),"%s",id);
    tars_motion_result_t result=tars_motion_submit(&cmd);
    if(result==TARS_MOTION_OK) send_event("command.ack",id,"accepted",NULL,false,NULL);
    else send_event("command.ack",id,"rejected",result==TARS_MOTION_DISARMED ? "disarmed" :
        result==TARS_MOTION_ESTOP ? "estop_latched" : "busy_or_unsafe",false,NULL);
}

static void handle_execute(const char *command_id, cJSON *payload) {
    cJSON *name = cJSON_GetObjectItemCaseSensitive(payload, "name");
    int steps, speed;
    if (!cJSON_IsString(name) || !json_int_range(payload, "steps", 1, 20, &steps) ||
        !json_int_range(payload, "speed_pct", 10, 100, &speed)) {
        send_event("command.ack", command_id, "rejected", "invalid_payload", false, NULL); return;
    }
    unsigned emotion=tars_emotion_find(name->valuestring);
    if(emotion) {
        const tars_emotion_plan_t *plan=tars_emotion_get(emotion);
        cJSON *support=cJSON_GetObjectItemCaseSensitive(payload,"support_confirm");
        if(steps!=1 || speed!=100 || (support && !cJSON_IsBool(support)) ||
           (plan->requires_support && !cJSON_IsTrue(support))) {
            send_event("command.ack",command_id,"rejected","invalid_emotion_options",false,NULL); return;
        }
        tars_motion_command_t command={.repeat=1,.emotion_id=emotion,
            .emotion_support_confirm=cJSON_IsTrue(support)};
        snprintf(command.command_id,sizeof(command.command_id),"%s",command_id);
        tars_motion_result_t result=tars_motion_submit(&command);
        if(result==TARS_MOTION_OK) send_event("command.ack",command_id,"accepted",NULL,false,NULL);
        else send_event("command.ack",command_id,"rejected",
            result==TARS_MOTION_DISARMED?"disarmed":result==TARS_MOTION_ESTOP?"estop_latched":"busy_or_unsafe",false,NULL);
        return;
    }
    tars_action_t action;
    if (strcmp(name->valuestring, "walk_forward") == 0) action = TARS_ACTION_FORWARD;
    else if (strcmp(name->valuestring, "turn_left") == 0) action = TARS_ACTION_LEFT;
    else if (strcmp(name->valuestring, "turn_right") == 0) action = TARS_ACTION_RIGHT;
    else { send_event("command.ack", command_id, "rejected", "invalid_motion", false, NULL); return; }

    tars_motion_command_t command = {.action = action, .repeat = (uint8_t)steps,
                                     .frame_ms = TARS_GAIT_HOLD_MS};
    /* speed_pct remains validated for JSON v2 compatibility. The original
     * user-tuned gait has fixed timing; percentage must not alter its rhythm. */
    snprintf(command.command_id, sizeof(command.command_id), "%s", command_id);
    tars_motion_result_t result = tars_motion_submit(&command);
    if (result == TARS_MOTION_OK) send_event("command.ack", command_id, "accepted", NULL, false, NULL);
    else send_event("command.ack", command_id, "rejected",
                    result == TARS_MOTION_BUSY ? "busy" : result == TARS_MOTION_ESTOP ? "estop_latched" :
                    result == TARS_MOTION_DISARMED ? "disarmed" : "invalid_motion",
                    result == TARS_MOTION_BUSY, NULL);
}

static void send_state(const char *command_id) {
    tars_snapshot_t s = tars_state_get();
    cJSON *payload = cJSON_CreateObject();
    cJSON *result = cJSON_AddObjectToObject(payload, "result");
    cJSON_AddNumberToObject(result, "sequence", s.sequence);
    cJSON_AddNumberToObject(result, "lifecycle", s.lifecycle);
    cJSON_AddNumberToObject(result, "interaction", s.interaction);
    cJSON_AddNumberToObject(result, "motion", s.motion);
    cJSON_AddNumberToObject(result, "health", s.health);
    cJSON_AddBoolToObject(result, "connected", s.connected);
    cJSON_AddBoolToObject(result, "armed", tars_motion_is_armed());
    cJSON_AddStringToObject(result, "active_command_id", s.active_command_id);
    uint32_t audio_reason, audio_frames, audio_watchdogs; int audio_error;
    tars_wifi_audio_diagnostic(&audio_reason,&audio_error,&audio_frames,&audio_watchdogs);
    cJSON_AddNumberToObject(result,"audio_reason",audio_reason);
    cJSON_AddNumberToObject(result,"audio_error",audio_error);
    cJSON_AddNumberToObject(result,"audio_frames",audio_frames);
    cJSON_AddNumberToObject(result,"audio_watchdogs",audio_watchdogs);
    cJSON_AddNumberToObject(result,"wifi_rssi",tars_wifi_audio_rssi());
    uint32_t ws_stage; int ws_error;
    tars_xiaozhi_audio_diagnostic(&ws_stage,&ws_error);
    cJSON_AddNumberToObject(result,"ws_stage",ws_stage);
    cJSON_AddNumberToObject(result,"ws_error",ws_error);
    send_event("command.completed", command_id, "completed", NULL, false, payload);
}

/* Explicit calibration surface. Regular keepalive never renews this lease. */
static void handle_calibration(const char *type, const char *id, cJSON *payload) {
    bool ok = true;
    cJSON *lease = cJSON_GetObjectItemCaseSensitive(payload, "calibration_id");
    if (!strcmp(type, "calibration.begin")) {
        int servo;
        cJSON *confirm = cJSON_GetObjectItemCaseSensitive(payload, "confirm");
        ok = cJSON_IsTrue(confirm) && valid_command_id(lease) &&
             json_int_range(payload, "servo", 1, 4, &servo);
        if (ok) ok = !atomic_load(&s_audio_job_active) && !tars_audio_is_busy() &&
                     !tars_wifi_audio_session_active() && tars_calibration_begin(servo, lease->valuestring);
    } else if (!strcmp(type, "calibration.set")) {
        int angle;
        ok = valid_command_id(lease) && json_int_range(payload, "angle_deg", -20, 20, &angle);
        if (ok) ok = tars_calibration_set(lease->valuestring, angle);
    } else if (!strcmp(type, "calibration.renew")) {
        ok = valid_command_id(lease) && tars_calibration_renew(lease->valuestring);
    } else if (!strcmp(type, "calibration.end")) {
        tars_calibration_end();
    } else if (strcmp(type, "calibration.status")) {
        send_event("command.ack", id, "rejected", "unknown_type", false, NULL); return;
    }
    if (!ok) { send_event("command.ack", id, "rejected", "calibration_denied", false, NULL); return; }
    send_event("command.ack", id, "accepted", NULL, false, NULL);
    tars_calibration_status_t s = tars_calibration_status();
    cJSON *response = cJSON_CreateObject();
    cJSON *data = cJSON_AddObjectToObject(response, "result");
    cJSON_AddBoolToObject(data, "active", s.active);
    cJSON_AddBoolToObject(data, "timed_out", s.timed_out);
    cJSON_AddBoolToObject(data, "estop", s.estop);
    cJSON_AddNumberToObject(data, "servo", s.servo);
    cJSON_AddNumberToObject(data, "angle_deg", s.angle_deg);
    cJSON_AddNumberToObject(data, "pulse_us", s.pulse_us);
    send_event("command.completed", id, "completed", NULL, false, response);
}

static void audio_test_worker(void *arg) {
    (void)arg;
    audio_job_t job = s_audio_job;
    tars_audio_self_test_result_t result;
    esp_err_t err = tars_audio_run_self_test(&job.config, &result);
    /* No result from an abandoned application session may complete a new job. */
    if (!s_session_active || strcmp(job.session_id, s_session_id) != 0) {
        atomic_store(&s_audio_job_active, false); tars_calibration_release_audio(); vTaskDelete(NULL); return;
    }
    if (err != ESP_OK) {
        tars_audio_abort();
        send_event("command.failed", job.command_id, "failed", "audio_self_test_failed", true, NULL);
    } else {
        cJSON *response = cJSON_CreateObject();
        cJSON *data = cJSON_AddObjectToObject(response, "result");
        cJSON_AddNumberToObject(data, "sample_rate_hz", result.sample_rate_hz);
        cJSON_AddNumberToObject(data, "mic_samples", result.mic_samples);
        cJSON_AddNumberToObject(data, "mic_rms", result.mic_rms);
        cJSON_AddNumberToObject(data, "mic_peak", result.mic_peak);
        cJSON_AddNumberToObject(data, "mic_dc", result.mic_dc);
        cJSON_AddNumberToObject(data, "mic_clipped_samples", result.mic_clipped_samples);
        cJSON_AddBoolToObject(data, "mic_signal_detected", result.mic_signal_detected);
        cJSON_AddBoolToObject(data, "tone_played", result.tone_played);
        send_event("command.completed", job.command_id, "completed", NULL, false, response);
    }
    atomic_store(&s_audio_job_active, false);
    tars_calibration_release_audio();
    vTaskDelete(NULL);
}

static void handle_audio_self_test(const char *command_id, cJSON *payload) {
    if (atomic_load(&s_audio_job_active) || tars_wifi_audio_session_active()) {
        send_event("command.ack", command_id, "rejected", "audio_busy", true, NULL); return;
    }
    if (tars_motion_is_armed() || tars_motion_is_busy() || tars_calibration_status().active) {
        send_event("command.ack", command_id, "rejected", "disarm_required", false, NULL);
        return;
    }
    int mic_ms, tone_ms, tone_hz, volume_pct;
    if (!json_int_range(payload, "mic_ms", 500, 3000, &mic_ms) ||
        !json_int_range(payload, "tone_ms", 200, 1000, &tone_ms) ||
        !json_int_range(payload, "tone_hz", 200, 2000, &tone_hz) ||
        !json_int_range(payload, "volume_pct", 1, 5, &volume_pct)) {
        send_event("command.ack", command_id, "rejected", "invalid_payload", false, NULL);
        return;
    }
    tars_audio_self_test_config_t config = {
        .mic_duration_ms = (uint16_t)mic_ms,
        .tone_duration_ms = (uint16_t)tone_ms,
        .tone_frequency_hz = (uint16_t)tone_hz,
        .tone_volume_pct = (uint8_t)volume_pct,
    };
    if (!tars_calibration_reserve_audio()) {
        send_event("command.ack", command_id, "rejected", "calibration_active", false, NULL); return;
    }
    if (tars_audio_recover() != ESP_OK) {
        tars_calibration_release_audio();
        send_event("command.ack", command_id, "rejected", "audio_busy", true, NULL);
        return;
    }
    s_audio_job.config = config;
    snprintf(s_audio_job.command_id, sizeof(s_audio_job.command_id), "%s", command_id);
    snprintf(s_audio_job.session_id, sizeof(s_audio_job.session_id), "%s", s_session_id);
    atomic_store(&s_audio_job_active, true);
    send_event("command.ack", command_id, "accepted", NULL, false, NULL);
    if (xTaskCreate(audio_test_worker, "audio_test", 6144, NULL, 3, NULL) != pdPASS) {
        atomic_store(&s_audio_job_active, false);
        tars_calibration_release_audio();
        send_event("command.failed", command_id, "failed", "audio_worker_unavailable", true, NULL);
    }
}

static void handle_line(const char *line, size_t length) {
    if (!line || length == 0 || length > TARS_PROTOCOL_MAX_LINE) {
        send_event("command.ack", NULL, "rejected", length > TARS_PROTOCOL_MAX_LINE ? "line_too_long" : "invalid_json", false, NULL); return;
    }
    if (!valid_utf8(line, length, false)) {
        send_event("command.ack", NULL, "rejected", "invalid_utf8", false, NULL); return;
    }
    cJSON *root = cJSON_ParseWithLength(line, length);
    if (!cJSON_IsObject(root)) { cJSON_Delete(root); send_event("command.ack", NULL, "rejected", "invalid_json", false, NULL); return; }
    cJSON *v = cJSON_GetObjectItemCaseSensitive(root, "v");
    cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
    cJSON *id = cJSON_GetObjectItemCaseSensitive(root, "command_id");
    cJSON *payload = cJSON_GetObjectItemCaseSensitive(root, "payload");
    cJSON *target_boot_id = cJSON_GetObjectItemCaseSensitive(root, "target_boot_id");
    if (!cJSON_IsNumber(v) || v->valuedouble != 2.0 || !cJSON_IsString(type) || !valid_command_id(id) || !cJSON_IsObject(payload)) {
        send_event("command.ack", valid_command_id(id) ? id->valuestring : NULL, "rejected", "missing_field", false, NULL);
        cJSON_Delete(root); return;
    }
    if (strcmp(type->valuestring, "session.open") == 0) {
        memset(&s_emotion_upload,0,sizeof(s_emotion_upload));
        cJSON *session = cJSON_GetObjectItemCaseSensitive(payload, "session_id");
        if (!valid_command_id(session)) {
            send_event("command.ack", id->valuestring, "rejected", "invalid_session", false, NULL);
            cJSON_Delete(root); return;
        }
        if (s_session_active && s_input_wifi==atomic_load(&s_owner_wifi) && strcmp(session->valuestring, s_session_id) == 0) {
            s_last_rx_ms = esp_timer_get_time() / 1000;
            send_hello();
            cJSON_Delete(root); return;
        }
        /* Reopening control cannot kill independent voice; Arm is never restored. */
        if(!tars_wifi_audio_independent()) tars_audio_abort();
        tars_motion_disarm();
        tars_usb_begin_session();
        atomic_store(&s_owner_wifi,s_input_wifi);
        snprintf(s_session_id, sizeof(s_session_id), "%s", session->valuestring);
        s_session_active = true;
        s_last_rx_ms = esp_timer_get_time() / 1000;
        tars_state_set_connected(true);
        send_hello();
        cJSON_Delete(root); return;
    }
    /* A second controller cannot renew the active owner's lease or execute. */
    if(s_input_wifi!=atomic_load(&s_owner_wifi)) { cJSON_Delete(root); return; }
    cJSON *session = cJSON_GetObjectItemCaseSensitive(root, "session_id");
    if (!s_session_active || !cJSON_IsString(session) ||
        strcmp(session->valuestring, s_session_id) != 0) {
        send_event("command.ack", id->valuestring, "rejected", "session_required", false, NULL);
        cJSON_Delete(root); return;
    }
    if (!cJSON_IsString(target_boot_id) || strcmp(target_boot_id->valuestring, s_boot_id) != 0) {
        send_event("command.ack", id->valuestring, "rejected", "boot_id_mismatch", false, NULL);
        cJSON_Delete(root); return;
    }
    tars_motion_renew_lease();
    s_last_rx_ms = esp_timer_get_time() / 1000;
    tars_state_set_connected(true);
    if (strcmp(type->valuestring, "session.keepalive") == 0) {
        cJSON_Delete(root); return;
    }
    char replay[RESPONSE_MAX];
    int cached = cache_lookup_or_add(id->valuestring, line, length, replay);
    if (cached == 1) { if (replay[0]) send_raw(replay); cJSON_Delete(root); return; }
    if (cached < 0) { send_event("command.ack", id->valuestring, "rejected", cached == -1 ? "command_id_conflict" : "queue_full", cached == -2, NULL); cJSON_Delete(root); return; }

    if (s_input_wifi && (strncmp(type->valuestring,"calibration.",12)==0 || strncmp(type->valuestring,"motion.test_",12)==0 ||
            strcmp(type->valuestring,"audio.wifi.configure")==0 || strcmp(type->valuestring,"audio.self_test")==0)) {
        send_event("command.ack",id->valuestring,"rejected","physical_usb_required",false,NULL);
    }
    else if (strcmp(type->valuestring, "audio.wifi.configure") == 0) {
        cJSON *ssid=cJSON_GetObjectItemCaseSensitive(payload,"ssid");
        cJSON *password=cJSON_GetObjectItemCaseSensitive(payload,"password");
        cJSON *host=cJSON_GetObjectItemCaseSensitive(payload,"host");
        cJSON *token=cJSON_GetObjectItemCaseSensitive(payload,"token");
        int port;
        if(tars_motion_is_armed() || tars_motion_is_busy() || atomic_load(&s_audio_job_active) ||
           !cJSON_IsString(ssid) || !cJSON_IsString(password) || !cJSON_IsString(host) ||
           !cJSON_IsString(token) || !json_int_range(payload,"port",1024,65535,&port)) {
            send_event("command.ack",id->valuestring,"rejected","invalid_or_unsafe_wifi_config",false,NULL);
        } else {
            esp_err_t err=tars_wifi_audio_configure(ssid->valuestring,password->valuestring,host->valuestring,port,token->valuestring);
            if(err!=ESP_OK) send_event("command.ack",id->valuestring,"rejected","wifi_config_not_saved",false,NULL);
            else {
                send_event("command.ack",id->valuestring,"accepted",NULL,false,NULL);
                send_event("command.completed",id->valuestring,"completed",NULL,false,NULL);
                /* Applied only after a user-controlled reboot. Never echo credentials. */
            }
        }
    }
    else if (strcmp(type->valuestring, "interaction.set") == 0) {
        cJSON *state = cJSON_GetObjectItemCaseSensitive(payload, "state");
        const char *names[] = {"IDLE", "LISTENING", "THINKING", "SPEAKING"};
        int value = -1;
        for (int i = 0; i < 4; ++i) if (cJSON_IsString(state) && strcmp(state->valuestring, names[i]) == 0) value = i;
        if (value < 0) send_event("command.ack", id->valuestring, "rejected", "invalid_payload", false, NULL);
        else {
            tars_state_set_interaction((tars_interaction_t)value);
            send_event("command.ack", id->valuestring, "accepted", NULL, false, NULL);
            send_event("command.completed", id->valuestring, "completed", NULL, false, NULL);
        }
    }
    else if (strcmp(type->valuestring,"capabilities.get")==0) {
        cJSON *p=cJSON_CreateObject(),*r=cJSON_AddObjectToObject(p,"result");
        cJSON_AddNumberToObject(r,"director_api",1);
        cJSON_AddStringToObject(r,"display_modes","pixel,legacy");
        cJSON_AddNumberToObject(r,"expression_count",ORBIT_COUNT);
        cJSON_AddBoolToObject(r,"expression_hold",true);
        cJSON_AddBoolToObject(r,"audio_stop",true);
        cJSON_AddBoolToObject(r,"motion_audio_serial",true);
        send_event("command.completed",id->valuestring,"completed",NULL,false,p);
    }
    else if (strcmp(type->valuestring,"display.mode")==0) {
        cJSON *mode=cJSON_GetObjectItemCaseSensitive(payload,"mode");
        if(!cJSON_IsString(mode) || (strcmp(mode->valuestring,"pixel") && strcmp(mode->valuestring,"legacy")))
            send_event("command.ack",id->valuestring,"rejected","invalid_display_mode",false,NULL);
        else {
            tars_display_set_legacy(!strcmp(mode->valuestring,"legacy"));
            send_event("command.completed",id->valuestring,"completed",NULL,false,NULL);
        }
    }
    else if (strcmp(type->valuestring,"display.catalog")==0) {
        int index;
        if(!json_int_range(payload,"index",0,ORBIT_COUNT-1,&index))
            send_event("command.ack",id->valuestring,"rejected","invalid_index",false,NULL);
        else {
            cJSON *p=cJSON_CreateObject(),*r=cJSON_AddObjectToObject(p,"result");
            cJSON_AddNumberToObject(r,"count",ORBIT_COUNT);cJSON_AddStringToObject(r,"name",orbit_name(index));
            cJSON_AddNumberToObject(r,"duration_ms",orbit_duration(index));cJSON_AddBoolToObject(r,"action",orbit_is_action(index));
            send_event("command.completed",id->valuestring,"completed",NULL,false,p);
        }
    }
    else if (strcmp(type->valuestring,"display.expression")==0 || strcmp(type->valuestring,"display.release")==0) {
        uint32_t ticket=0;
        if(strcmp(type->valuestring,"display.release")==0) ticket=tars_orbit_release();
        else {
            cJSON *name=cJSON_GetObjectItemCaseSensitive(payload,"name"); int duration;
            if(cJSON_IsString(name) && json_int_range(payload,"duration_ms",0,3600000,&duration))
                ticket=tars_orbit_direct(name->valuestring,duration);
        }
        if(!ticket) send_event("command.ack",id->valuestring,"rejected","invalid_expression_or_queue_full",false,NULL);
        else {
            cJSON *p=cJSON_CreateObject(),*r=cJSON_AddObjectToObject(p,"result");
            cJSON_AddNumberToObject(r,"ticket",ticket);
            send_event("command.ack",id->valuestring,"accepted",NULL,false,NULL);
            send_event("command.completed",id->valuestring,"completed",NULL,false,p);
        }
    }
    else if (strcmp(type->valuestring,"display.status")==0) {
        uint32_t applied;int current;bool manual;tars_orbit_status(&applied,&current,&manual);
        cJSON *p=cJSON_CreateObject(),*r=cJSON_AddObjectToObject(p,"result");
        cJSON_AddNumberToObject(r,"applied",applied);cJSON_AddStringToObject(r,"name",orbit_name(current));
        cJSON_AddBoolToObject(r,"manual",manual);
        cJSON_AddStringToObject(r,"mode",tars_display_is_legacy()?"legacy":"pixel");
        send_event("command.ack",id->valuestring,"accepted",NULL,false,NULL);
        send_event("command.completed",id->valuestring,"completed",NULL,false,p);
    }
    else if (strcmp(type->valuestring,"audio.stop")==0) {
        tars_audio_abort();
        send_event("command.ack",id->valuestring,"accepted",NULL,false,NULL);
        send_event("command.completed",id->valuestring,"completed",NULL,false,NULL);
    }
    else if (strcmp(type->valuestring,"motion.test_plan")==0) handle_test_plan(id->valuestring,payload);
    else if (strcmp(type->valuestring,"motion.test_run")==0) handle_test_run(id->valuestring,payload);
    else if (strcmp(type->valuestring,"motion.catalog")==0) handle_emotion_plan(id->valuestring);
    else if (strcmp(type->valuestring,"motion.test_emotion_plan")==0) handle_emotion_plan(id->valuestring);
    else if (strcmp(type->valuestring,"motion.test_emotion_run")==0) handle_emotion_run(id->valuestring,payload);
    else if (strncmp(type->valuestring,"motion.test_emotion_",20)==0) handle_emotion_edit(type->valuestring,id->valuestring,payload);
    else if (strncmp(type->valuestring, "calibration.", 12) == 0) handle_calibration(type->valuestring, id->valuestring, payload);
    else if (strcmp(type->valuestring, "motion.execute") == 0) handle_execute(id->valuestring, payload);
    else if (strcmp(type->valuestring, "dialogue.append") == 0) handle_dialogue(id->valuestring, payload);
    else if (strcmp(type->valuestring, "audio.self_test") == 0) handle_audio_self_test(id->valuestring, payload);
    else if (strcmp(type->valuestring, "motion.cancel") == 0) {
        cJSON *target = cJSON_GetObjectItemCaseSensitive(payload, "target_command_id");
        if (!valid_command_id(target)) send_event("command.ack", id->valuestring, "rejected", "invalid_payload", false, NULL);
        else if (!tars_motion_cancel(target->valuestring)) send_event("command.ack", id->valuestring, "rejected", "target_not_found", false, NULL);
        else { send_event("command.ack", id->valuestring, "accepted", NULL, false, NULL); send_event("command.completed", id->valuestring, "completed", NULL, false, NULL); }
    } else if (strcmp(type->valuestring, "safety.estop") == 0) {
        tars_audio_abort();
        tars_motion_estop(); send_event("command.ack", id->valuestring, "accepted", NULL, false, NULL); send_event("command.completed", id->valuestring, "completed", NULL, false, NULL);
    } else if (strcmp(type->valuestring, "safety.clear") == 0) {
        if (!tars_motion_clear_estop()) send_event("command.ack", id->valuestring, "rejected", "busy", true, NULL);
        else { send_event("command.ack", id->valuestring, "accepted", NULL, false, NULL); send_event("command.completed", id->valuestring, "completed", NULL, false, NULL); }
    } else if (strcmp(type->valuestring, "safety.arm") == 0) {
        if (tars_calibration_status().active) send_event("command.ack", id->valuestring, "rejected", "calibration_active", false, NULL);
        else if (atomic_load(&s_audio_job_active) || tars_audio_is_busy()) send_event("command.ack", id->valuestring, "rejected", "audio_busy", true, NULL);
        else if (!tars_motion_arm()) send_event("command.ack", id->valuestring, "rejected", "estop_latched", false, NULL);
        else { send_event("command.ack", id->valuestring, "accepted", NULL, false, NULL); send_event("command.completed", id->valuestring, "completed", NULL, false, NULL); }
    } else if (strcmp(type->valuestring, "safety.disarm") == 0) {
        memset(&s_emotion_upload,0,sizeof(s_emotion_upload));
        if(!tars_wifi_audio_independent()) tars_audio_abort();
        tars_motion_disarm(); send_event("command.ack", id->valuestring, "accepted", NULL, false, NULL); send_event("command.completed", id->valuestring, "completed", NULL, false, NULL);
    } else if (strcmp(type->valuestring, "audio.transport") == 0) {
        char info[300]; tars_xiaozhi_audio_details(info,sizeof(info));
        cJSON *result=cJSON_Parse(info);
        cJSON *reply=cJSON_CreateObject(); cJSON_AddItemToObject(reply,"result",result);
        send_event("command.ack",id->valuestring,"accepted",NULL,false,NULL);
        send_event("command.completed",id->valuestring,"completed",NULL,false,reply);
    } else if (strcmp(type->valuestring, "diagnostic.get") == 0) {
        cJSON *item=cJSON_GetObjectItemCaseSensitive(payload,"view");
        const char *view=cJSON_IsString(item) ? item->valuestring : "boot";
        if((item && !cJSON_IsString(item)) || (strcmp(view,"boot") && strcmp(view,"current") && strcmp(view,"previous"))) {
            send_event("command.ack",id->valuestring,"rejected","invalid_payload",false,NULL);
        } else {
            cJSON *p=cJSON_CreateObject(); cJSON *result=cJSON_AddObjectToObject(p,"result");
            cJSON_AddStringToObject(result,"view",view);
            if(!strcmp(view,"boot")) {
                tars_diag_mark_t previous;
                cJSON_AddNumberToObject(result,"reset_reason",tars_diag_reset_reason());
                cJSON_AddStringToObject(result,"reset_name",tars_diag_reset_name());
                cJSON_AddNumberToObject(result,"uptime_ms",esp_timer_get_time()/1000);
                cJSON_AddBoolToObject(result,"previous_valid",tars_diag_get(true,&previous));
                cJSON_AddStringToObject(result,"retention","best_effort_RTC");
            } else {
                tars_diag_mark_t mark;
                bool valid=tars_diag_get(!strcmp(view,"previous"),&mark);
                cJSON_AddBoolToObject(result,"valid",valid);
                if(valid) {
                    cJSON_AddStringToObject(result,"stage",tars_diag_stage_name(mark.stage));
                    cJSON_AddNumberToObject(result,"action",mark.action);
                    cJSON_AddNumberToObject(result,"cycle",mark.cycle);
                    cJSON_AddNumberToObject(result,"phase",mark.phase);
                    cJSON_AddNumberToObject(result,"mark_ms",mark.uptime_ms);
                    cJSON_AddStringToObject(result,"command_id",mark.command_id);
                }
            }
            send_event("command.ack",id->valuestring,"accepted",NULL,false,NULL);
            send_event("command.completed",id->valuestring,"completed",NULL,false,p);
        }
    } else if (strcmp(type->valuestring, "state.get") == 0) {
        send_event("command.ack", id->valuestring, "accepted", NULL, false, NULL); send_state(id->valuestring);
    } else send_event("command.ack", id->valuestring, "rejected", "unknown_type", false, NULL);
    cJSON_Delete(root);
}

static void dispatch_line(const char *line,size_t length,bool wifi) {
    xSemaphoreTake(s_handler_lock,portMAX_DELAY);
    s_input_wifi=wifi;
    handle_line(line,length);
    xSemaphoreGive(s_handler_lock);
}
void tars_protocol_handle_line(const char *line,size_t length) { dispatch_line(line,length,false); }
static void wifi_line(const char *line,size_t length) { dispatch_line(line,length,true); }
static void wifi_lost(void) {
    xSemaphoreTake(s_handler_lock,portMAX_DELAY);
    if(atomic_load(&s_owner_wifi)) link_lost();
    xSemaphoreGive(s_handler_lock);
}

static void rx_task(void *arg) {
    (void)arg;
    uint8_t chunk[128];
    char line[TARS_PROTOCOL_MAX_LINE + sizeof(TARS_USB_PROTOCOL_PREFIX)];
    size_t used = 0;
    bool overflow = false;
    while (true) {
        int count = tars_usb_read(chunk, sizeof(chunk));
        xSemaphoreTake(s_handler_lock,portMAX_DELAY);
        bool usb_lost=!tars_usb_link_ok();
        bool expired=s_session_active && esp_timer_get_time()/1000-s_last_rx_ms>2000;
        if (expired || (usb_lost && !atomic_load(&s_owner_wifi))) {
            if(s_session_active) link_lost();
            else if(!atomic_load(&s_owner_wifi)) tars_usb_begin_session();
        }
        xSemaphoreGive(s_handler_lock);
        if (usb_lost || expired) {
            used = 0; overflow = false;
            continue;
        }
        for (int i = 0; i < count; ++i) {
            if (chunk[i] == '\n') {
                if (overflow) send_event("command.ack", NULL, "rejected", "line_too_long", false, NULL);
                else if (used >= strlen(TARS_USB_PROTOCOL_PREFIX) &&
                         memcmp(line, TARS_USB_PROTOCOL_PREFIX, strlen(TARS_USB_PROTOCOL_PREFIX)) == 0)
                    tars_protocol_handle_line(line + strlen(TARS_USB_PROTOCOL_PREFIX),
                                              used - strlen(TARS_USB_PROTOCOL_PREFIX));
                used = 0; overflow = false; continue;
            }
            if (chunk[i] == '\r') continue;
            if (used < sizeof(line) - 1) line[used++] = (char)chunk[i]; else overflow = true;
        }
    }
}

static void send_hello(void) {
    char hello[512];
    snprintf(hello, sizeof(hello),
             "{\"v\":2,\"type\":\"hello\",\"boot_id\":\"%s\",\"payload\":{\"device_id\":\"tars-01\",\"protocols\":[2],\"firmware_version\":\"%s\",\"release_id\":\"%s\",\"boot_disarmed\":true,\"control_lease_ms\":2000,\"servo_calibration_v1\":true,\"buffered_audio_v1\":true,\"director_api\":1,\"session_id\":\"%s\"}}",
             s_boot_id, TARS_FIRMWARE_VERSION, TARS_RELEASE_ID, s_session_id);
    send_raw(hello);
}

esp_err_t tars_protocol_init(void) {
    s_cache_lock = xSemaphoreCreateMutex();
    s_tx_lock = xSemaphoreCreateMutex();
    s_handler_lock = xSemaphoreCreateMutex();
    if (!s_cache_lock || !s_tx_lock || !s_handler_lock) return ESP_ERR_NO_MEM;
    tars_wifi_audio_set_control_handlers(wifi_line,wifi_lost);
    snprintf(s_boot_id, sizeof(s_boot_id), "%08lx%08lx",
             (unsigned long)esp_random(), (unsigned long)esp_random());
    tars_motion_set_event_callback(motion_event);
    if (xTaskCreate(rx_task, "protocol_rx", 6144, NULL, 6, NULL) != pdPASS) return ESP_ERR_NO_MEM;
    send_hello();
    return ESP_OK;
}
