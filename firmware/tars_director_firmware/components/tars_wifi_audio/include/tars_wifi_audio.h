#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

/* Configuration is provisioned through physical USB, stored in NVS and applied
   only on the next reboot. No automatic reset or NVS erase. */
esp_err_t tars_wifi_audio_init(void);
esp_err_t tars_wifi_audio_configure(const char *ssid, const char *password,
                                  const char *host, int port, const char *token);
bool tars_wifi_audio_session_active(void);
/* JSON v2 is dispatched by the existing protocol module, never interpreted here.
 * Callbacks avoid a circular component dependency. Queueing is nonblocking. */
void tars_wifi_audio_set_control_handlers(void (*receive)(const char *, size_t),
                                          void (*disconnect)(void));
bool tars_wifi_audio_send_control(const char *json);
void tars_wifi_audio_drop_session(void);
void tars_wifi_audio_diagnostic(uint32_t *reason, int *error, uint32_t *frames, uint32_t *watchdogs);
int tars_wifi_audio_rssi(void);

bool tars_wifi_audio_independent(void);
bool tars_wifi_audio_endpoint(char *host, size_t host_size, char *token, size_t token_size);
