#include "tars_state.h"

#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_timer.h"

static tars_snapshot_t s_state;
static SemaphoreHandle_t s_lock;
static bool s_usb_connected, s_audio_connected;

static void bump(void) {
    s_state.sequence++;
    s_state.timestamp_ms = esp_timer_get_time() / 1000;
}

void tars_state_init(void) {
    s_lock = xSemaphoreCreateMutex();
    s_usb_connected = false;
    s_audio_connected = false;
    s_state = (tars_snapshot_t) {
        .lifecycle = TARS_LIFECYCLE_READY,
        .interaction = TARS_INTERACTION_IDLE,
        .motion = TARS_MOTION_STANDING,
        .health = TARS_HEALTH_DEGRADED,
    };
    bump();
}

tars_snapshot_t tars_state_get(void) {
    tars_snapshot_t copy;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    copy = s_state;
    xSemaphoreGive(s_lock);
    return copy;
}

void tars_state_set_motion(tars_motion_state_t motion, const char *command_id) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_state.motion = motion;
    snprintf(s_state.active_command_id, sizeof(s_state.active_command_id), "%s", command_id ? command_id : "");
    bump();
    xSemaphoreGive(s_lock);
}

void tars_state_set_health(tars_health_t health) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_state.health = health;
    bump();
    xSemaphoreGive(s_lock);
}

void tars_state_set_connected(bool connected) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_usb_connected = connected;
    s_state.connected = s_usb_connected || s_audio_connected;
    bump();
    xSemaphoreGive(s_lock);
}

void tars_state_set_audio_connected(bool connected) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_audio_connected = connected;
    s_state.connected = s_usb_connected || s_audio_connected;
    bump();
    xSemaphoreGive(s_lock);
}

void tars_state_set_interaction(tars_interaction_t interaction) {
    if (interaction < TARS_INTERACTION_IDLE || interaction > TARS_INTERACTION_SPEAKING) return;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_state.interaction = interaction;
    bump();
    xSemaphoreGive(s_lock);
}
