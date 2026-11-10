#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "tars_emotion.h"
#define TARS_EMOTION_MAX_FRAMES 16

#define TARS_SERVO_COUNT 4
#define TARS_COMMAND_ID_MAX 40
#define TARS_GAIT_HOLD_MS 300 /* user's original tars_run phase timing */
#define TARS_GAIT_PHASES 6
#define TARS_TEST_MAX_PHASES 12

typedef enum { TARS_ACTION_FORWARD, TARS_ACTION_LEFT, TARS_ACTION_RIGHT } tars_action_t;
typedef enum { TARS_MOTION_OK, TARS_MOTION_BUSY, TARS_MOTION_INVALID, TARS_MOTION_ESTOP,
               TARS_MOTION_DISARMED } tars_motion_result_t;
typedef enum { TARS_MOTION_EVENT_COMPLETED, TARS_MOTION_EVENT_CANCELLED, TARS_MOTION_EVENT_ESTOPPED } tars_motion_event_t;
typedef void (*tars_motion_event_callback_t)(const char *command_id, tars_motion_event_t event);

typedef struct {
    char command_id[TARS_COMMAND_ID_MAX];
    tars_action_t action;
    uint8_t repeat;
    uint8_t emotion_id; /* Experimental fixed catalog; 0 means normal gait. */
    bool emotion_support_confirm;
    uint8_t emotion_count;
    tars_emotion_frame_t emotion_frames[TARS_EMOTION_MAX_FRAMES];
    uint16_t frame_ms; /* compatibility: only 0 or TARS_GAIT_HOLD_MS; no retiming */
    uint8_t test_count; /* 0: unchanged normal gait; 1..12: USB operator prefix test */
    bool test_drop_ramp; /* USB lab only: forward phase4, 300ms total */
    uint8_t test_smooth_mask; /* forward:8; turns A:8 B:28 C:62, or direct:0 */
    uint8_t test_settle_ms; /* optional turn phase4 post-trajectory hold:0/60 */
    int16_t test_frames[TARS_TEST_MAX_PHASES][TARS_SERVO_COUNT];
} tars_motion_command_t;

typedef struct {
    int16_t zero_deg;
    int8_t direction;
    int16_t physical_min_deg;
    int16_t physical_max_deg;
    int gpio;
} tars_servo_calibration_t;

extern const tars_servo_calibration_t TARS_SERVO_CALIBRATION[TARS_SERVO_COUNT];

void tars_motion_init(void);
tars_motion_result_t tars_motion_submit(const tars_motion_command_t *command);
bool tars_motion_cancel(const char *command_id);
void tars_motion_estop(void);
bool tars_motion_clear_estop(void);
bool tars_motion_arm(void);
void tars_motion_disarm(void);
void tars_motion_renew_lease(void);
bool tars_motion_is_armed(void);
bool tars_motion_is_busy(void);
void tars_motion_set_event_callback(tars_motion_event_callback_t callback);
int16_t tars_motion_logical_to_physical(uint8_t servo, int16_t logical_deg);
void tars_motion_forward_plan(int16_t frames[TARS_GAIT_PHASES][TARS_SERVO_COUNT]);
bool tars_motion_gait_plan(tars_action_t action,int16_t frames[TARS_GAIT_PHASES][TARS_SERVO_COUNT]);
void tars_motion_test_extra_limits(tars_action_t action,int servo,int *lower,int *upper);
bool tars_motion_test_valid(const tars_motion_command_t *command);
bool tars_motion_emotion_valid(const tars_motion_command_t *command);

void tars_servo_init(void);
void tars_servo_write_physical(const int16_t angles[TARS_SERVO_COUNT]);
void tars_servo_write_physical_float(const float angles[TARS_SERVO_COUNT]);
void tars_servo_output_enable(void);
void tars_servo_output_disable(void);

/* Reference coordinates only: no historical gait offsets/directions. */
typedef struct {
    bool active;
    bool timed_out;
    bool estop;
    int servo; /* 1..4, or 0 when off */
    int angle_deg;
    int pulse_us;
} tars_calibration_status_t;
bool tars_calibration_begin(int servo, const char *lease_id);
bool tars_calibration_set(const char *lease_id, int angle_deg);
bool tars_calibration_renew(const char *lease_id);
void tars_calibration_end(void);
tars_calibration_status_t tars_calibration_status(void);
void tars_servo_calibration_write(int servo, int angle_deg);
/* Audio admission shares the calibration gate, closing cross-task check/use races. */
bool tars_calibration_reserve_audio(void);
void tars_calibration_release_audio(void);
