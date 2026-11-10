#pragma once
#include "esp_err.h"
#include <stdint.h>
esp_err_t tars_xiaozhi_audio_init(void);
void tars_xiaozhi_audio_diagnostic(uint32_t *stage,int *error);
void tars_xiaozhi_audio_details(char *out,size_t capacity);
