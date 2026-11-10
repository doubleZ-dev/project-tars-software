#pragma once
#include <stdint.h>
/* Integral of a 60/180/60ms trapezoidal velocity profile, duration 300ms.
 * Integer-degree command targets, not measured shaft positions. */
static inline int16_t tars_drop_target(int16_t start, int16_t end, unsigned ms) {
    if (ms >= 300) return end;
    float u=(float)ms/300.0f;
    float s=u<0.2f ? u*u/0.32f : u<=0.8f ? (u-0.1f)/0.8f : 1.0f-(1.0f-u)*(1.0f-u)/0.32f;
    float scaled=(end-start)*s;
    int32_t offset=(int32_t)(scaled>=0 ? scaled+0.5f : scaled-0.5f);
    return start + offset;
}
