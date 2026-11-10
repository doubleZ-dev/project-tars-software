#pragma once
#include <stdint.h>
#include <stdbool.h>

typedef enum {
#define ORBIT_ENTRY(id, name, ms, action) ORBIT_##id,
#include "orbit_catalog.def"
#undef ORBIT_ENTRY
    ORBIT_COUNT
} orbit_id_t;
enum { ORBIT_NONE, ORBIT_STAR, ORBIT_HEART, ORBIT_TEAR, ORBIT_SWEAT,
       ORBIT_QUESTION, ORBIT_DOTS, ORBIT_VOICE, ORBIT_BATTERY,
       ORBIT_WARNING, ORBIT_ZZZ, ORBIT_SCAN, ORBIT_MOTION };
typedef struct {
    int32_t x, y, w, h, brow_y, tilt, gaze, curve, blush, decor, strength, blink, color;
} orbit_pose_t;
/* Rect: x/y top left, a/b width/height, radius. Line: x/y -> a/b, width. */
typedef struct { int32_t kind, x, y, a, b, radius, width, color, opacity; } orbit_primitive_t;
#define ORBIT_MAX_PRIMITIVES 128
typedef struct { uint32_t count; orbit_primitive_t items[ORBIT_MAX_PRIMITIVES]; } orbit_frame_t;
typedef struct {
    int32_t state, action;
    uint32_t state_started, action_started, action_duration;
} orbit_control_t;

const char *orbit_name(orbit_id_t id);
int orbit_find(const char *name);
uint32_t orbit_duration(orbit_id_t id);
bool orbit_is_action(orbit_id_t id);
void orbit_sample(orbit_id_t id, unsigned eye, uint32_t elapsed, uint32_t duration,
                  unsigned voice_level, orbit_pose_t *out);
void orbit_blend(const orbit_pose_t *from, const orbit_pose_t *to, unsigned progress,
                 orbit_pose_t *out); /* 0..256, geometry-only smooth transition */
void orbit_frame(const orbit_pose_t *pose, unsigned eye, uint32_t elapsed, orbit_frame_t *out);
void orbit_control_init(orbit_control_t *c, uint32_t now);
bool orbit_control_state(orbit_control_t *c, orbit_id_t state, uint32_t now);
bool orbit_control_action(orbit_control_t *c, orbit_id_t action, uint32_t duration, uint32_t now);
void orbit_control_cancel(orbit_control_t *c);
orbit_id_t orbit_control_current(orbit_control_t *c, uint32_t now, uint32_t *elapsed, uint32_t *duration);
