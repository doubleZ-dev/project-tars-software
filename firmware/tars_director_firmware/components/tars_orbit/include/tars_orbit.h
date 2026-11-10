#pragma once
#include "lvgl.h"
#include "orbit_model.h"

/* Init once under LVGL lock. Eye 0 routes to CS18, eye 1 to CS6.
 * Post functions are nonblocking, thread safe, and return false on invalid/full queue.
 * They never manipulate motion hardware or LVGL outside the display task.
 * Actions may be suppressed by boot/error/low-battery/sleep after enqueue. */
void tars_orbit_init(lv_obj_t *cs18_screen, lv_obj_t *cs6_screen, bool gallery);
bool tars_orbit_set_state(orbit_id_t state);
bool tars_orbit_play_action(const char *name, uint32_t duration_ms);
bool tars_orbit_cancel_action(void);
bool tars_orbit_set_voice_level(uint8_t level);
bool tars_orbit_set_gallery(bool enabled);

/* Director overrides are consumed on the LVGL task. duration 0 loops until released.
 * get_status reports applied (render model selected), not physical LCD scanout. */
uint32_t tars_orbit_direct(const char *name, uint32_t duration_ms);
uint32_t tars_orbit_release(void);
void tars_orbit_status(uint32_t *applied, int *current, bool *manual);
void tars_orbit_set_visible(bool visible);
