/*
 * TARS Left Display — AI Consciousness Screen
 * 128×106 visible area starting at LVGL y=54
 * Layer: Background RGB565 image + LVGL dynamic overlay elements
 */
#pragma once
#include "lvgl.h"
#include "tars_ui_state.h"

void tars_left_display_init(lv_obj_t *parent);
void tars_left_display_set_state(tars_left_state_t state);
void tars_left_display_update_step(uint16_t step);
void tars_left_display_update_gait_phase(const char *phase);
void tars_left_display_update_error(const char *module, int recoverable);
