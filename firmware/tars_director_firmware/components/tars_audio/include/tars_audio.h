#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include "driver/gpio.h"
#include "esp_err.h"
#include "tars_board.h"

/*
 * PCB V1 uses independent I2S0 RX and I2S1 TX clocks.
 * Stage 4 creates channels lazily on explicit request, never on boot.
 */
#define TARS_AUDIO_MIC_I2S_PORT TARS_BOARD_MIC_I2S_PORT
#define TARS_AUDIO_MIC_BCLK_GPIO TARS_BOARD_MIC_BCLK_GPIO
#define TARS_AUDIO_MIC_WS_GPIO TARS_BOARD_MIC_WS_GPIO
#define TARS_AUDIO_MIC_DATA_GPIO TARS_BOARD_MIC_DIN_GPIO
#define TARS_AUDIO_SPK_I2S_PORT TARS_BOARD_SPK_I2S_PORT
#define TARS_AUDIO_SPK_BCLK_GPIO TARS_BOARD_SPK_BCLK_GPIO
#define TARS_AUDIO_SPK_WS_GPIO TARS_BOARD_SPK_WS_GPIO
#define TARS_AUDIO_SPK_DATA_GPIO TARS_BOARD_SPK_DOUT_GPIO
#define TARS_AUDIO_AMP_SD_GPIO TARS_BOARD_AMP_SD_GPIO
#define TARS_AUDIO_AMP_GAIN_GPIO TARS_BOARD_AMP_GAIN_GPIO
#if !TARS_BOARD_AUDIO_CLOCKS_INDEPENDENT
/* Historical shared-clock implementation below is excluded on PCB V1. */
#define TARS_AUDIO_I2S_BCLK_GPIO TARS_AUDIO_MIC_BCLK_GPIO
#define TARS_AUDIO_I2S_WS_GPIO TARS_AUDIO_MIC_WS_GPIO
#endif
#define TARS_AUDIO_SAMPLE_RATE_HZ 16000

/* INMP441 L/R is wired to GND, so samples are carried in the left I2S slot. */
#define TARS_AUDIO_MIC_LEFT_SLOT TARS_BOARD_MIC_LEFT_SLOT

typedef struct {
    uint16_t mic_duration_ms;
    uint16_t tone_duration_ms;
    uint16_t tone_frequency_hz;
    uint8_t tone_volume_pct;
} tars_audio_self_test_config_t;

typedef struct {
    uint32_t sample_rate_hz;
    uint32_t mic_samples;
    uint32_t mic_rms;
    uint32_t mic_peak;
    int32_t mic_dc;
    uint32_t mic_clipped_samples;
    bool mic_signal_detected;
    bool tone_played;
} tars_audio_self_test_result_t;

/* Establishes safe GPIO state. I2S initializes lazily on explicit self-test/LAN request. */
esp_err_t tars_audio_init(void);
/* Reserve I2S DMA before TLS/codec startup; channels remain disabled and muted. */
esp_err_t tars_audio_prepare(void);

/* Amplifier enable is allowed only during an initialized TX operation. */
esp_err_t tars_audio_amp_set_enabled(bool enabled);
bool tars_audio_amp_is_enabled(void);

/* Half-duplex API; start/read-or-write/finish must use one task. The exclusive
   lock is held until stop/finish. abort is thread-safe and immediately mutes;
   the owner must unwind/stop within the bounded I/O timeout. Recover only when idle. */
bool tars_audio_is_busy(void);
esp_err_t tars_audio_recover(void);
void tars_audio_abort(void);
esp_err_t tars_audio_capture_start(void);
esp_err_t tars_audio_capture_read(int16_t *pcm, size_t capacity, size_t *samples);
void tars_audio_capture_stop(void);
esp_err_t tars_audio_play_start(uint8_t volume_pct);
esp_err_t tars_audio_play_write(const int16_t *pcm, size_t samples);
esp_err_t tars_audio_play_finish(void);

/* Explicit bounded diagnostic; statistics are in PCM16 units. Never call from
   the JSON RX task: use a worker so estop/cancel remain responsive. */
esp_err_t tars_audio_run_self_test(const tars_audio_self_test_config_t *config,
                                  tars_audio_self_test_result_t *result);
