#pragma once

#include <stddef.h>
#include "esp_err.h"

#define TARS_PROTOCOL_MAX_LINE 512

esp_err_t tars_protocol_init(void);
void tars_protocol_handle_line(const char *line, size_t length);
