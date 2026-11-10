#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef enum { TARS_LIFECYCLE_BOOT, TARS_LIFECYCLE_READY, TARS_LIFECYCLE_SLEEP } tars_lifecycle_t;
typedef enum { TARS_INTERACTION_IDLE, TARS_INTERACTION_LISTENING, TARS_INTERACTION_THINKING, TARS_INTERACTION_SPEAKING } tars_interaction_t;
typedef enum { TARS_MOTION_STANDING, TARS_MOTION_EXECUTING, TARS_MOTION_STOPPING, TARS_MOTION_FAULT } tars_motion_state_t;
typedef enum { TARS_HEALTH_OK, TARS_HEALTH_DEGRADED, TARS_HEALTH_ESTOP, TARS_HEALTH_FAULT } tars_health_t;

typedef struct {
    uint32_t sequence;
    int64_t timestamp_ms;
    tars_lifecycle_t lifecycle;
    tars_interaction_t interaction;
    tars_motion_state_t motion;
    tars_health_t health;
    bool connected;
    char active_command_id[40];
} tars_snapshot_t;

void tars_state_init(void);
tars_snapshot_t tars_state_get(void);
void tars_state_set_motion(tars_motion_state_t motion, const char *command_id);
void tars_state_set_health(tars_health_t health);
void tars_state_set_connected(bool connected);
void tars_state_set_audio_connected(bool connected);
void tars_state_set_interaction(tars_interaction_t interaction);
