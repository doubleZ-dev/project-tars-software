#pragma once
#include <stdint.h>

/* INMP441: signed 24-bit sample occupies bits 31..8 of the LEFT 32-bit slot.
   Discard its low 8 valid bits to obtain signed PCM16; never use the right slot. */
static inline int16_t tars_audio_left32_to_pcm16(int32_t word) {
    int32_t value = (uint32_t)word >> 16;
    if (value >= 32768) value -= 65536;
    return (int16_t)value;
}

static inline int32_t tars_audio_pcm16_to_slot(int16_t sample, unsigned volume) {
    int32_t scaled = (int32_t)sample * (int32_t)volume / 100;
    return scaled * 65536; /* multiplication avoids left-shifting negative values */
}
