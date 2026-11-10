#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#define TARS_USB_PROTOCOL_PREFIX "@TARS2 "
#define TARS_USB_LOG_PREFIX "@LOG2 "
esp_err_t tars_usb_init(void);
bool tars_usb_send_protocol(const char *json);
int tars_usb_read(void *data, size_t size);
bool tars_usb_link_ok(void);
void tars_usb_begin_session(void);
