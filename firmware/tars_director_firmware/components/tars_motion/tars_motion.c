#include "tars_motion.h"
#include "tars_board.h"
#include "tars_drop_trajectory.h"
#include "tars_emotion.h"

#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "tars_state.h"
#include "tars_diagnostics.h"

#define MOTION_TICK_MS 20
#define MAX_REPEAT 20
#define CONTROL_LEASE_TIMEOUT_MS 2000

static int64_t now_ms(void);

static const char *TAG = "tars_motion";

typedef struct { int16_t logical[TARS_SERVO_COUNT]; } gait_frame_t;

/* Logical coordinates: +forward for 1/4 and +up for 2/3. */
static const gait_frame_t GAIT_FORWARD[] = {
    {{ 0,  0,  0,  0}}, {{ 0, 45, 45,  0}}, {{ 5, 45, 45,  5}},
    /* Latest user test defaults:5-degree sides, phase4 middle -30. */
    {{ 5,-30,-30,  5}}, {{ 0,-30,-30,  0}}, {{ 0,  0,  0,  0}},
};
static const gait_frame_t GAIT_RIGHT[] = {
    {{ 0,  0,  0,  0}}, {{ 0,-30,-30,  0}}, {{-45,-30,-30, 45}},
    {{-45, 25, 35, 45}}, {{ 0, 25, 35,  0}}, {{ 0,  0,  0,  0}},
};
static const gait_frame_t GAIT_LEFT[] = {
    {{ 0,  0,  0,  0}}, {{ 0,-30,-30,  0}}, {{40,-30,-30,-40}},
    {{40, 20, 20,-40}}, {{ 0, 20, 20,  0}}, {{ 0,  0,  0,  0}},
};

static const gait_frame_t *gait_plan(tars_action_t action) {
    if(action==TARS_ACTION_FORWARD) return GAIT_FORWARD;
    if(action==TARS_ACTION_LEFT) return GAIT_LEFT;
    if(action==TARS_ACTION_RIGHT) return GAIT_RIGHT;
    return NULL;
}
bool tars_motion_gait_plan(tars_action_t action,int16_t frames[TARS_GAIT_PHASES][TARS_SERVO_COUNT]) {
    const gait_frame_t *plan=gait_plan(action);
    if(!plan || !frames) return false;
    for(int f=0;f<TARS_GAIT_PHASES;++f) memcpy(frames[f],plan[f].logical,sizeof(frames[f]));
    return true;
}
void tars_motion_forward_plan(int16_t frames[TARS_GAIT_PHASES][TARS_SERVO_COUNT]) {
    (void)tars_motion_gait_plan(TARS_ACTION_FORWARD,frames);
}
void tars_motion_test_extra_limits(tars_action_t action,int servo,int *lower,int *upper) {
    (void)action; (void)servo;
    *lower=-45; *upper=45;
}
bool tars_motion_test_valid(const tars_motion_command_t *c) {
    if(!c || !gait_plan(c->action) || c->repeat!=1 || c->test_count<1 ||
       c->test_count>TARS_TEST_MAX_PHASES) return false;
    unsigned smooth=c->test_smooth_mask | (c->test_drop_ramp ? 8u : 0u);
    if(c->test_drop_ramp && c->action!=TARS_ACTION_FORWARD) return false;
    if(c->action==TARS_ACTION_FORWARD) {
        if(smooth!=0 && smooth!=8) return false;
        if(c->test_settle_ms) return false;
    } else {
        if(smooth!=0 && smooth!=8 && smooth!=28 && smooth!=62) return false;
        if(c->test_settle_ms && (c->test_settle_ms!=60 || !(smooth&8))) return false;
        /* Keep support axes fixed, independent of A/B/C trajectory choice. */
        if(smooth) for(int f=2;f<5 && f<c->test_count;++f) {
            int first=f==3 ? 0 : 1, second=f==3 ? 3 : 2;
            if(c->test_frames[f][first]!=c->test_frames[f-1][first] ||
               c->test_frames[f][second]!=c->test_frames[f-1][second]) return false;
        }
    }
    if(c->action==TARS_ACTION_FORWARD && smooth) {
        if(c->test_count>=4 && (c->test_frames[2][0]!=c->test_frames[3][0] ||
                               c->test_frames[2][3]!=c->test_frames[3][3])) return false;
    }
    for(int f=0;f<c->test_count;++f) for(int j=0;j<TARS_SERVO_COUNT;++j) {
        int value=c->test_frames[f][j];
        /* USB laboratory: every cell editable inside the same bounded envelope. */
        int lower=-45,upper=45;
        if(value<lower || value>upper) return false;
        const tars_servo_calibration_t *cal=&TARS_SERVO_CALIBRATION[j];
        int physical=cal->zero_deg+cal->direction*value;
        if(physical<cal->physical_min_deg || physical>cal->physical_max_deg) return false;
    }
    return true;
}

/* Assembly zeros explicitly approved by user on 2026-11-07; reference coordinates. */
const tars_servo_calibration_t TARS_SERVO_CALIBRATION[TARS_SERVO_COUNT] = {
    {.zero_deg=10,  .direction= 1, .physical_min_deg=-90, .physical_max_deg=90, .gpio=TARS_BOARD_SERVO1_GPIO},
    {.zero_deg=0,   .direction=-1, .physical_min_deg=-90, .physical_max_deg=90, .gpio=TARS_BOARD_SERVO2_GPIO},
    {.zero_deg=-10, .direction= 1, .physical_min_deg=-90, .physical_max_deg=90, .gpio=TARS_BOARD_SERVO3_GPIO},
    {.zero_deg=2,   .direction=-1, .physical_min_deg=-90, .physical_max_deg=90, .gpio=TARS_BOARD_SERVO4_GPIO},
};

#define CALIBRATION_LEASE_MS 1200
static SemaphoreHandle_t s_gate;
static tars_calibration_status_t s_cal;
static char s_cal_id[TARS_COMMAND_ID_MAX];
static int64_t s_cal_deadline;
static unsigned s_audio_reservations;
static void calibration_off(bool timeout) {
    s_cal.active = false;
    s_cal.servo = 0;
    s_cal.timed_out = timeout;
    s_cal_id[0] = 0;
    s_cal_deadline = 0;
    tars_servo_output_disable();
}
static void calibration_expire(void) {
    if (s_cal.active && now_ms() >= s_cal_deadline) calibration_off(true);
}
static QueueHandle_t s_queue;
static TaskHandle_t s_task;
static volatile bool s_cancel;
static volatile bool s_estop;
static volatile bool s_armed;
static volatile int64_t s_lease_deadline_ms;
static char s_active_id[TARS_COMMAND_ID_MAX];
static tars_motion_event_callback_t s_event_callback;

static int64_t now_ms(void) { return esp_timer_get_time() / 1000; }

int16_t tars_motion_logical_to_physical(uint8_t servo, int16_t logical_deg) {
    if (servo >= TARS_SERVO_COUNT) return 0;
    const tars_servo_calibration_t *c = &TARS_SERVO_CALIBRATION[servo];
    int physical = c->zero_deg + c->direction * logical_deg;
    if (physical < c->physical_min_deg) physical = c->physical_min_deg;
    if (physical > c->physical_max_deg) physical = c->physical_max_deg;
    return (int16_t)physical;
}

static int16_t s_last_logical[TARS_SERVO_COUNT]; /* last command, not measured angle */
static void write_logical(const int16_t logical[TARS_SERVO_COUNT]) {
    int16_t physical[TARS_SERVO_COUNT];
    for (int i = 0; i < TARS_SERVO_COUNT; ++i) physical[i] = tars_motion_logical_to_physical(i, logical[i]);
    tars_servo_write_physical(physical);
    memcpy(s_last_logical,logical,sizeof(s_last_logical));
}

static void neutral(void) {
    const int16_t zero[TARS_SERVO_COUNT] = {0, 0, 0, 0};
    write_logical(zero);
}

static void stop_sequence(const tars_motion_command_t *cmd) {
    if(cmd->test_count || cmd->emotion_id) {
        /* A partial test must NEVER add an unselected neutral/return phase. */
        if(!s_estop) tars_motion_disarm();
    } else neutral();
}

static bool emotion_active(void) {
    return !s_cancel && !s_estop && s_armed && now_ms()<=s_lease_deadline_ms;
}

static bool execute_emotion(const tars_motion_command_t *cmd) {
    const tars_emotion_plan_t *plan=tars_emotion_get(cmd->emotion_id);
    const tars_emotion_plan_t custom={.name="custom",.count=cmd->emotion_count,.frames=cmd->emotion_frames};
    if(cmd->emotion_count) plan=&custom;
    if(!plan) return false;
    /* Same queue and stop generation as gait; no independent task or auto-replay. */
    for(unsigned f=0;f<plan->count;++f) {
        const tars_emotion_frame_t *frame=&plan->frames[f];
        int16_t from[4]; memcpy(from,s_last_logical,sizeof(from));
        if(!emotion_active()) { tars_motion_disarm(); return false; }
        tars_diag_mark(TARS_DIAG_TARGET,100+cmd->emotion_id,1,f+1,cmd->command_id);
        int64_t started=now_ms();
        TickType_t wake=xTaskGetTickCount();
        unsigned scheduled=0;
        while(scheduled<frame->move_ms) {
            unsigned step=frame->move_ms-scheduled;
            if(step>MOTION_TICK_MS) step=MOTION_TICK_MS;
            vTaskDelayUntil(&wake,pdMS_TO_TICKS(step)); scheduled+=step;
            if(!emotion_active()) { tars_motion_disarm(); return false; }
            unsigned elapsed=(unsigned)(now_ms()-started);
            float physical[4];
            for(int j=0;j<4;++j) {
                float q=tars_emotion_target(from[j],frame->logical[j],elapsed,frame->move_ms);
                const tars_servo_calibration_t *c=&TARS_SERVO_CALIBRATION[j];
                physical[j]=c->zero_deg+c->direction*q;
                if(physical[j]<c->physical_min_deg) physical[j]=c->physical_min_deg;
                if(physical[j]>c->physical_max_deg) physical[j]=c->physical_max_deg;
                s_last_logical[j]=(int16_t)(q>=0?q+0.5f:q-0.5f);
            }
            tars_servo_write_physical_float(physical);
        }
        if(!emotion_active()) { tars_motion_disarm(); return false; }
        float endpoint[4];
        for(int j=0;j<4;++j) endpoint[j]=TARS_SERVO_CALIBRATION[j].zero_deg+
            TARS_SERVO_CALIBRATION[j].direction*frame->logical[j];
        tars_servo_write_physical_float(endpoint); /* Same PWM rounding as trajectory. */
        memcpy(s_last_logical,frame->logical,sizeof(s_last_logical));
        tars_diag_mark(TARS_DIAG_HOLD,100+cmd->emotion_id,1,f+1,cmd->command_id);
        wake=xTaskGetTickCount();
        for(unsigned held=0;held<frame->hold_ms;) {
            unsigned step=frame->hold_ms-held;
            if(step>MOTION_TICK_MS) step=MOTION_TICK_MS;
            vTaskDelayUntil(&wake,pdMS_TO_TICKS(step)); held+=step;
            if(!emotion_active()) { tars_motion_disarm(); return false; }
        }
    }
    /* Normal sequences end at N, then explicitly release PWM like the USB lab. */
    tars_motion_disarm();
    return true;
}

static bool execute(const tars_motion_command_t *cmd) {
    if(cmd->emotion_id) return execute_emotion(cmd);
    const gait_frame_t *frames = GAIT_FORWARD;
    size_t count = sizeof(GAIT_FORWARD) / sizeof(GAIT_FORWARD[0]);
    if (cmd->action == TARS_ACTION_LEFT) { frames = GAIT_LEFT; count = sizeof(GAIT_LEFT) / sizeof(GAIT_LEFT[0]); }
    if (cmd->action == TARS_ACTION_RIGHT) { frames = GAIT_RIGHT; count = sizeof(GAIT_RIGHT) / sizeof(GAIT_RIGHT[0]); }
    if(cmd->test_count) count=cmd->test_count;
    /* Voice/normal actions: all six phases smooth; USB lab keeps comparisons. */
    unsigned smooth=cmd->test_count ? cmd->test_smooth_mask | (cmd->test_drop_ramp ? 8u : 0u) :
                    63u;

    for (uint8_t repeat = 0; repeat < cmd->repeat; ++repeat) {
        for (size_t f = 0; f < count; ++f) {
            if (s_cancel || s_estop) { stop_sequence(cmd); return false; }
            tars_diag_mark(TARS_DIAG_TARGET,cmd->action,repeat+1,f+1,cmd->command_id);
            if(f<TARS_GAIT_PHASES && (smooth & (1u<<f))) {
                /* Shared executor, previous commanded endpoint, common clock. */
                int16_t from[TARS_SERVO_COUNT];
                const int16_t *previous=f ? (cmd->test_count ? cmd->test_frames[f-1] : frames[f-1].logical) : s_last_logical;
                memcpy(from,previous,sizeof(from));
                const int16_t *to=cmd->test_count ? cmd->test_frames[f] : frames[f].logical;
                TickType_t wake=xTaskGetTickCount();
                tars_diag_mark(TARS_DIAG_HOLD,cmd->action,repeat+1,f+1,cmd->command_id);
                for(unsigned ms=MOTION_TICK_MS;ms<=TARS_GAIT_HOLD_MS;ms+=MOTION_TICK_MS) {
                    vTaskDelayUntil(&wake,pdMS_TO_TICKS(MOTION_TICK_MS));
                    if(s_cancel || s_estop) { stop_sequence(cmd); return false; }
                    int16_t target[TARS_SERVO_COUNT];
                    for(int j=0;j<TARS_SERVO_COUNT;++j) target[j]=tars_drop_target(from[j],to[j],ms);
                    write_logical(target);
                }
                if(cmd->test_count && f==3) for(unsigned ms=0;ms<cmd->test_settle_ms;ms+=MOTION_TICK_MS) {
                    vTaskDelayUntil(&wake,pdMS_TO_TICKS(MOTION_TICK_MS));
                    if(s_cancel || s_estop) { stop_sequence(cmd); return false; }
                }
                continue;
            }
            /* Match user's original execute_sequence: one direct target write,
             * then 300ms hold. Never interpolate or stretch gait phases.
             * Subdivide ONLY the wait so cancel/lease/estop stay responsive. */
            if(cmd->test_count) write_logical(cmd->test_frames[f]);
            else write_logical(frames[f].logical);
            tars_diag_mark(TARS_DIAG_HOLD,cmd->action,repeat+1,f+1,cmd->command_id);
            TickType_t wake = xTaskGetTickCount();
            for (unsigned elapsed = 0; elapsed < TARS_GAIT_HOLD_MS; elapsed += MOTION_TICK_MS) {
                if (s_cancel || s_estop) { stop_sequence(cmd); return false; }
                vTaskDelayUntil(&wake, pdMS_TO_TICKS(MOTION_TICK_MS));
            }
        }
    }
    if(cmd->test_count) {
        tars_motion_disarm(); /* after selected phase300ms; no automatic zero move */
    } else neutral();
    return true;
}

static void motion_task(void *arg) {
    (void)arg;
    tars_motion_command_t cmd;
    while (true) {
        if (xQueueReceive(s_queue, &cmd, portMAX_DELAY) != pdTRUE) continue;
        tars_state_set_motion(TARS_MOTION_EXECUTING, s_active_id);
        int diagnostic_action=cmd.emotion_id ? 100+cmd.emotion_id : cmd.action;
        tars_diag_mark(TARS_DIAG_GAIT_START,diagnostic_action,0,0,cmd.command_id);
        bool completed = execute(&cmd);
        tars_diag_mark_t last;
        tars_diag_get(false,&last);
        tars_diag_mark(completed ? TARS_DIAG_COMPLETE : TARS_DIAG_CANCELLED,diagnostic_action,last.cycle,last.phase,cmd.command_id);
        ESP_LOGI(TAG, "command=%s result=%s", s_active_id, completed ? "completed" : "cancelled");
        if (s_event_callback) {
            s_event_callback(s_active_id, completed ? TARS_MOTION_EVENT_COMPLETED :
                             (s_estop ? TARS_MOTION_EVENT_ESTOPPED : TARS_MOTION_EVENT_CANCELLED));
        }
        s_active_id[0] = '\0';
        tars_state_set_motion(s_estop ? TARS_MOTION_FAULT : TARS_MOTION_STANDING, NULL);
    }
}

static void lease_watchdog_task(void *arg) {
    (void)arg;
    while (true) {
        xSemaphoreTake(s_gate, portMAX_DELAY);
        calibration_expire();
        xSemaphoreGive(s_gate);
        if (s_armed && now_ms() > s_lease_deadline_ms) {
            ESP_LOGW(TAG, "control lease expired; disabling servo outputs");
            tars_motion_disarm();
            tars_state_set_connected(false);
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

void tars_motion_init(void) {
    s_gate = xSemaphoreCreateMutex();
    configASSERT(s_gate);
    tars_servo_init();
    tars_servo_output_disable();
    s_armed = false;
    s_lease_deadline_ms = 0;
    s_queue = xQueueCreate(4, sizeof(tars_motion_command_t));
    xTaskCreate(motion_task, "motion_task", 4096, NULL, 8, &s_task);
    xTaskCreate(lease_watchdog_task, "motion_lease", 3072, NULL, 9, NULL);
}

bool tars_motion_emotion_valid(const tars_motion_command_t *c) {
    if(!c || !c->emotion_count || c->emotion_count>TARS_EMOTION_MAX_FRAMES ||
       c->emotion_id!=255 || c->repeat!=1 || c->test_count || c->test_drop_ramp ||
       c->test_smooth_mask || c->test_settle_ms) return false;
    unsigned total=0; int16_t previous[4]={0};
    for(unsigned i=0;i<c->emotion_count;++i) {
        const tars_emotion_frame_t *f=&c->emotion_frames[i];
        if(f->move_ms<200 || f->move_ms>3000 || f->hold_ms>3000 || f->move_ms%10 || f->hold_ms%10 ||
           f->logical[1]!=f->logical[2]) return false;
        total+=f->move_ms+f->hold_ms;
        bool moving_sides=f->logical[0]!=previous[0] || f->logical[3]!=previous[3];
        if(moving_sides && (previous[1]!=-30 || previous[2]!=-30 ||
                           f->logical[1]!=-30 || f->logical[2]!=-30)) return false;
        if((f->logical[0] || f->logical[3]) && (f->logical[1]!=-30 || f->logical[2]!=-30)) return false;
        bool sides=previous[0] || previous[3] || f->logical[0] || f->logical[3];
        if(sides && (previous[1]<0 || f->logical[1]<0) && !c->emotion_support_confirm) return false;
        for(int j=0;j<4;++j) {
            int q=f->logical[j],lo=(j==1 || j==2)?-30:-20,hi=(j==1 || j==2)?30:20;
            const tars_servo_calibration_t *cal=&TARS_SERVO_CALIBRATION[j];
            int physical=cal->zero_deg+cal->direction*q;
            if(q<lo || q>hi || physical<cal->physical_min_deg || physical>cal->physical_max_deg) return false;
            previous[j]=q;
        }
    }
    return total<=30000 && !previous[0] && !previous[1] && !previous[2] && !previous[3];
}

tars_motion_result_t tars_motion_submit(const tars_motion_command_t *command) {
    if (!command || !command->command_id[0] || command->repeat == 0 || command->repeat > MAX_REPEAT) return TARS_MOTION_INVALID;
    if (command->frame_ms && command->frame_ms != TARS_GAIT_HOLD_MS) return TARS_MOTION_INVALID;
    if(command->test_count && !tars_motion_test_valid(command)) return TARS_MOTION_INVALID;
    if(command->emotion_count && !tars_motion_emotion_valid(command)) return TARS_MOTION_INVALID;
    if(command->emotion_id && !command->emotion_count) {
        const tars_emotion_plan_t *p=tars_emotion_get(command->emotion_id);
        if(!p || command->repeat!=1 || command->test_count || command->test_drop_ramp ||
           command->test_smooth_mask || command->test_settle_ms ||
           (p->requires_support && !command->emotion_support_confirm)) return TARS_MOTION_INVALID;
        if(p->count>TARS_EMOTION_MAX_FRAMES) return TARS_MOTION_INVALID;
        tars_motion_command_t check=*command;
        check.emotion_id=255;
        check.emotion_count=p->count;
        memcpy(check.emotion_frames,p->frames,p->count*sizeof(p->frames[0]));
        if(!tars_motion_emotion_valid(&check)) return TARS_MOTION_INVALID;
    }
    xSemaphoreTake(s_gate, portMAX_DELAY);
    tars_motion_result_t result=TARS_MOTION_BUSY;
    if (s_estop) { result=TARS_MOTION_ESTOP; goto done; }
    if (!s_armed) { result=TARS_MOTION_DISARMED; goto done; }
    if (s_audio_reservations || tars_motion_is_busy() || uxQueueMessagesWaiting(s_queue)) goto done;
    s_cancel = false;
    snprintf(s_active_id, sizeof(s_active_id), "%s", command->command_id);
    if (xQueueSend(s_queue, command, 0) == pdTRUE) { result=TARS_MOTION_OK; goto done; }
    s_active_id[0] = '\0';
done:
    xSemaphoreGive(s_gate);
    return result;
}

bool tars_motion_cancel(const char *command_id) {
    if (!command_id || !s_active_id[0] || strcmp(command_id, s_active_id) != 0) return false;
    s_cancel = true;
    return true;
}

void tars_motion_estop(void) {
    xSemaphoreTake(s_gate, portMAX_DELAY);
    calibration_off(false);
    s_armed = false;
    s_lease_deadline_ms = 0;
    s_estop = true;
    s_cancel = true;
    tars_servo_output_disable();
    tars_state_set_health(TARS_HEALTH_ESTOP);
    xSemaphoreGive(s_gate);
}

bool tars_motion_clear_estop(void) {
    /* Never let clear revoke the cancellation generation of an accepted motion. */
    if (tars_motion_is_busy()) return false;
    s_estop = false;
    s_cancel = false;
    tars_state_set_health(TARS_HEALTH_DEGRADED);
    return true;
}

bool tars_motion_arm(void) {
    xSemaphoreTake(s_gate, portMAX_DELAY);
    if (tars_motion_is_busy() || s_estop || s_cal.active || s_audio_reservations) {
        xSemaphoreGive(s_gate); return false;
    }
    tars_diag_mark(TARS_DIAG_ARM,-1,0,0,NULL);
    neutral();
    tars_servo_output_enable();
    s_lease_deadline_ms = now_ms() + CONTROL_LEASE_TIMEOUT_MS;
    s_armed = true;
    tars_state_set_health(TARS_HEALTH_OK);
    xSemaphoreGive(s_gate);
    return true;
}

void tars_motion_disarm(void) {
    xSemaphoreTake(s_gate, portMAX_DELAY);
    calibration_off(false);
    s_armed = false;
    s_lease_deadline_ms = 0;
    s_cancel = true;
    tars_servo_output_disable();
    tars_state_set_health(TARS_HEALTH_DEGRADED);
    if (!tars_motion_is_busy()) tars_state_set_motion(TARS_MOTION_STANDING, NULL);
    xSemaphoreGive(s_gate);
}

void tars_motion_renew_lease(void) {
    if (s_armed) s_lease_deadline_ms = now_ms() + CONTROL_LEASE_TIMEOUT_MS;
}

bool tars_motion_is_armed(void) { return s_armed; }

bool tars_motion_is_busy(void) { return s_active_id[0] != '\0'; }
void tars_motion_set_event_callback(tars_motion_event_callback_t callback) { s_event_callback = callback; }

bool tars_calibration_begin(int servo, const char *lease_id) {
    xSemaphoreTake(s_gate, portMAX_DELAY);
    calibration_expire();
    bool ok = servo >= 1 && servo <= 4 && lease_id && lease_id[0] &&
              strlen(lease_id) < sizeof(s_cal_id) && !s_estop && !s_armed &&
              !s_cal.active && !s_audio_reservations && !tars_motion_is_busy() && !uxQueueMessagesWaiting(s_queue);
    if (ok) {
        s_cal = (tars_calibration_status_t){.active=true, .servo=servo, .pulse_us=1500};
        snprintf(s_cal_id, sizeof(s_cal_id), "%s", lease_id);
        tars_servo_calibration_write(servo, 0);
        s_cal_deadline = now_ms() + CALIBRATION_LEASE_MS;
    }
    xSemaphoreGive(s_gate);
    return ok;
}
bool tars_calibration_set(const char *lease_id, int angle_deg) {
    xSemaphoreTake(s_gate, portMAX_DELAY);
    calibration_expire();
    bool ok = s_cal.active && !s_estop && lease_id && !strcmp(lease_id, s_cal_id) &&
              angle_deg >= -20 && angle_deg <= 20 &&
              angle_deg >= TARS_SERVO_CALIBRATION[s_cal.servo-1].physical_min_deg &&
              angle_deg <= TARS_SERVO_CALIBRATION[s_cal.servo-1].physical_max_deg;
    if (ok) {
        tars_servo_calibration_write(s_cal.servo, angle_deg);
        s_cal.angle_deg = angle_deg;
        s_cal.pulse_us = 500 + (angle_deg + 90) * 2000 / 180;
        calibration_expire();
    }
    xSemaphoreGive(s_gate);
    return ok;
}
bool tars_calibration_renew(const char *lease_id) {
    xSemaphoreTake(s_gate, portMAX_DELAY);
    calibration_expire();
    bool ok = s_cal.active && lease_id && !strcmp(lease_id, s_cal_id);
    if (ok) s_cal_deadline = now_ms() + CALIBRATION_LEASE_MS;
    xSemaphoreGive(s_gate);
    return ok;
}
void tars_calibration_end(void) {
    xSemaphoreTake(s_gate, portMAX_DELAY);
    if (s_cal.active || (!s_armed && !tars_motion_is_busy())) calibration_off(false);
    xSemaphoreGive(s_gate);
}
tars_calibration_status_t tars_calibration_status(void) {
    xSemaphoreTake(s_gate, portMAX_DELAY);
    calibration_expire();
    tars_calibration_status_t result = s_cal;
    result.estop = s_estop;
    xSemaphoreGive(s_gate);
    return result;
}

bool tars_calibration_reserve_audio(void) {
    xSemaphoreTake(s_gate, portMAX_DELAY);
    calibration_expire();
    /* An explicitly armed but stationary robot may listen. Moving may not. */
    bool ok = !s_cal.active && !s_estop && !s_audio_reservations && !tars_motion_is_busy() && !uxQueueMessagesWaiting(s_queue);
    if (ok) ++s_audio_reservations;
    xSemaphoreGive(s_gate);
    return ok;
}
void tars_calibration_release_audio(void) {
    xSemaphoreTake(s_gate, portMAX_DELAY);
    if (s_audio_reservations) --s_audio_reservations;
    xSemaphoreGive(s_gate);
}
