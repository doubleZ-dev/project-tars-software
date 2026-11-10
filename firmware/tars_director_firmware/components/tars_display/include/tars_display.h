#pragma once

#include <stdbool.h>
#include <stddef.h>
#include "tars_state.h"

#define TARS_DIALOGUE_TEXT_MAX_BYTES 300

void tars_display_init(void);
bool tars_display_post_snapshot(const tars_snapshot_t *snapshot);
bool tars_display_post_dialogue(const char *role, const char *text, size_t text_length);
unsigned tars_display_dropped_updates(void);
void tars_display_set_legacy(bool legacy);
bool tars_display_is_legacy(void);
