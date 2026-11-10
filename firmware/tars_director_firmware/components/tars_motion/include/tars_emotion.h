#pragma once
#include <stdint.h>
#include <stdbool.h>
typedef struct {
    int16_t logical[4];
    uint16_t move_ms;
    uint16_t hold_ms;
} tars_emotion_frame_t;
typedef struct {
    const char *name;
    bool requires_support;
    unsigned count;
    const tars_emotion_frame_t *frames;
} tars_emotion_plan_t;
const char *tars_emotion_hash(void);
unsigned tars_emotion_count(void);
unsigned tars_emotion_find(const char *name);
const tars_emotion_plan_t *tars_emotion_get(unsigned id);
/* 20/60/20 percent velocity profile. Command targets, not measured positions. */
static inline float tars_emotion_target(float from,float to,unsigned elapsed,unsigned duration) {
    if(!duration || elapsed>=duration) return to;
    float u=(float)elapsed/(float)duration;
    float s=u<0.2f ? u*u/0.32f : u<=0.8f ? (u-0.1f)/0.8f : 1-(1-u)*(1-u)/0.32f;
    return from+(to-from)*s;
}
