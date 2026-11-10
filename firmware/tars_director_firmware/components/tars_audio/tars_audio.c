#include "tars_audio.h"
#include "tars_audio_pcm.h"
#include "tars_orbit.h"
#include <math.h>
#include <stdatomic.h>
#include <string.h>
#include "driver/i2s_std.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define FRAMES 128
#define IO_TIMEOUT_MS 100
#define DMA_COUNT 4
static i2s_chan_handle_t s_rx, s_tx;
static SemaphoreHandle_t s_lock;
static atomic_int s_mode; /* 0=idle, 1=RX, 2=TX; one owner, half duplex */
static atomic_bool s_abort, s_amp_enabled;
static uint8_t s_volume;
static uint32_t s_played;
static int16_t s_last;
static portMUX_TYPE s_amp_lock = portMUX_INITIALIZER_UNLOCKED;

bool tars_audio_amp_is_enabled(void) { return atomic_load(&s_amp_enabled); }
bool tars_audio_is_busy(void) { return atomic_load(&s_mode) != 0; }

esp_err_t tars_audio_amp_set_enabled(bool enabled) {
    portENTER_CRITICAL(&s_amp_lock);
    if (enabled && (atomic_load(&s_mode) != 2 || atomic_load(&s_abort))) {
        portEXIT_CRITICAL(&s_amp_lock);
        return ESP_ERR_INVALID_STATE;
    }
    esp_err_t err = gpio_set_level(TARS_AUDIO_AMP_SD_GPIO, enabled ? 1 : 0);
    if (err == ESP_OK) atomic_store(&s_amp_enabled, enabled);
    portEXIT_CRITICAL(&s_amp_lock);
    return err;
}

void tars_audio_abort(void) {
    portENTER_CRITICAL(&s_amp_lock);
    atomic_store(&s_abort, true);
    (void)gpio_set_level(TARS_AUDIO_AMP_SD_GPIO, 0);
    atomic_store(&s_amp_enabled, false);
    portEXIT_CRITICAL(&s_amp_lock);
}

esp_err_t tars_audio_recover(void) {
    if (!s_lock || xSemaphoreTake(s_lock, 0) != pdTRUE) return ESP_ERR_INVALID_STATE;
    atomic_store(&s_abort, false);
    xSemaphoreGive(s_lock);
    return ESP_OK;
}

esp_err_t tars_audio_init(void) {
    /* Preload low before output mode. Software cannot control the ROM/reset gap. */
    esp_err_t err = gpio_set_level(TARS_AUDIO_AMP_SD_GPIO, 0);
    if (err != ESP_OK) return err;
    gpio_config_t sd = {.pin_bit_mask = 1ULL << TARS_AUDIO_AMP_SD_GPIO,
        .mode = GPIO_MODE_OUTPUT, .pull_down_en = GPIO_PULLDOWN_ENABLE};
    err = gpio_config(&sd);
    if (err != ESP_OK) return err;
    gpio_config_t gain = {.pin_bit_mask = 1ULL << TARS_AUDIO_AMP_GAIN_GPIO,
        .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE};
    err = gpio_config(&gain); /* GAIN is high impedance, never PWM volume. */
    if (err != ESP_OK) return err;
    s_lock = xSemaphoreCreateMutex();
    return s_lock ? ESP_OK : ESP_ERR_NO_MEM;
}

static esp_err_t ensure_channel(bool tx) {
    i2s_chan_handle_t *handle = tx ? &s_tx : &s_rx;
    if (*handle) return ESP_OK;
    i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(
        tx ? TARS_AUDIO_SPK_I2S_PORT : TARS_AUDIO_MIC_I2S_PORT, I2S_ROLE_MASTER);
    channel.dma_desc_num = DMA_COUNT;
    channel.dma_frame_num = FRAMES;
    channel.auto_clear_after_cb = true; /* TX underrun emits silence, never repeats old PCM */
    esp_err_t err = i2s_new_channel(&channel, tx ? handle : NULL, tx ? NULL : handle);
    if (err != ESP_OK) return err;
    i2s_std_config_t cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(TARS_AUDIO_SAMPLE_RATE_HZ),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {.mclk = I2S_GPIO_UNUSED,
            .bclk = tx ? TARS_AUDIO_SPK_BCLK_GPIO : TARS_AUDIO_MIC_BCLK_GPIO,
            .ws = tx ? TARS_AUDIO_SPK_WS_GPIO : TARS_AUDIO_MIC_WS_GPIO,
            .dout = tx ? TARS_AUDIO_SPK_DATA_GPIO : I2S_GPIO_UNUSED,
            .din = tx ? I2S_GPIO_UNUSED : TARS_AUDIO_MIC_DATA_GPIO},
    };
    err = i2s_channel_init_std_mode(*handle, &cfg);
    if (err != ESP_OK) { i2s_del_channel(*handle); *handle = NULL; }
    return err;
}

esp_err_t tars_audio_prepare(void) {
    esp_err_t err=ensure_channel(false);
    return err==ESP_OK ? ensure_channel(true) : err;
}

static esp_err_t begin(bool tx) {
    if (!s_lock || xSemaphoreTake(s_lock, 0) != pdTRUE) return ESP_ERR_INVALID_STATE;
    esp_err_t err = atomic_load(&s_abort) ? ESP_ERR_INVALID_STATE : ensure_channel(tx);
    if (err == ESP_OK) err = i2s_channel_enable(tx ? s_tx : s_rx);
    if (err != ESP_OK) { xSemaphoreGive(s_lock); return err; }
    atomic_store(&s_mode, tx ? 2 : 1);
    return ESP_OK;
}

esp_err_t tars_audio_capture_start(void) {
    esp_err_t err = begin(false);
    if (err != ESP_OK) return err;
    /* Discard startup samples while amp remains muted. */
    int16_t discard[FRAMES];
    size_t count;
    for (int i = 0; i < 8 && err == ESP_OK; ++i)
        err = tars_audio_capture_read(discard, FRAMES, &count);
    if (err != ESP_OK) tars_audio_capture_stop();
    return err;
}

esp_err_t tars_audio_capture_read(int16_t *pcm, size_t capacity, size_t *samples) {
    if (samples) *samples = 0;
    if (!pcm || !samples || !capacity || capacity > FRAMES) return ESP_ERR_INVALID_ARG;
    if (atomic_load(&s_mode) != 1 || atomic_load(&s_abort)) return ESP_ERR_INVALID_STATE;
    int32_t raw[FRAMES * 2];
    size_t bytes = 0;
    esp_err_t err = i2s_channel_read(s_rx, raw, capacity * 8, &bytes, IO_TIMEOUT_MS);
    if (err != ESP_OK || bytes % 8) return err != ESP_OK ? err : ESP_ERR_INVALID_SIZE;
    if (atomic_load(&s_abort)) return ESP_ERR_INVALID_STATE;
    *samples = bytes / 8;
    if (!*samples) return ESP_ERR_TIMEOUT;
    for (size_t i = 0; i < *samples; ++i) pcm[i] = tars_audio_left32_to_pcm16(raw[i * 2]);
    return ESP_OK;
}

void tars_audio_capture_stop(void) {
    if (atomic_load(&s_mode) != 1) return;
    i2s_channel_disable(s_rx);
    atomic_store(&s_mode, 0);
    xSemaphoreGive(s_lock);
}

static esp_err_t write_slots(const int32_t *slots, size_t frames) {
    if (atomic_load(&s_abort)) return ESP_ERR_INVALID_STATE;
    size_t bytes = 0;
    esp_err_t err = i2s_channel_write(s_tx, slots, frames * 8, &bytes, IO_TIMEOUT_MS);
    return err != ESP_OK ? err : bytes == frames * 8 ? ESP_OK : ESP_ERR_INVALID_SIZE;
}

esp_err_t tars_audio_play_start(uint8_t volume_pct) {
    if (volume_pct > 100) return ESP_ERR_INVALID_ARG;
    esp_err_t err = begin(true);
    if (err != ESP_OK) return err;
    s_volume = volume_pct; s_played = 0; s_last = 0;
    int32_t zeros[FRAMES * 2] = {0};
    for (int i = 0; i < DMA_COUNT && err == ESP_OK; ++i) err = write_slots(zeros, FRAMES);
    if (err == ESP_OK) {
        vTaskDelay(pdMS_TO_TICKS(40) + 1);
        if (volume_pct) err = tars_audio_amp_set_enabled(true);
    }
    if (err != ESP_OK) { (void)tars_audio_play_finish(); }
    return err;
}

esp_err_t tars_audio_play_write(const int16_t *pcm, size_t samples) {
    if (!pcm || !samples || atomic_load(&s_mode) != 2) return ESP_ERR_INVALID_ARG;
    int32_t slots[FRAMES * 2];
    while (samples) {
        size_t count = samples > FRAMES ? FRAMES : samples;
        for (size_t i = 0; i < count; ++i) {
            int32_t value = pcm[i];
            if (s_played < FRAMES) value = value * (int32_t)s_played / FRAMES;
            s_last = (int16_t)value;
            int32_t slot = tars_audio_pcm16_to_slot(s_last, s_volume);
            slots[i * 2] = slot; slots[i * 2 + 1] = slot; /* both slots carry mono */
            ++s_played;
        }
        esp_err_t err = write_slots(slots, count);
        if (err != ESP_OK) { tars_audio_abort(); return err; }
        unsigned peak = 0;
        for (size_t j = 0; j < count; j++) {
            unsigned mag = pcm[j] < 0 ? -(int32_t)pcm[j] : pcm[j];
            if (mag > peak) peak = mag;
        }
        tars_orbit_set_voice_level(peak / 128 > 255 ? 255 : peak / 128);
        pcm += count; samples -= count;
    }
    return ESP_OK;
}

esp_err_t tars_audio_play_finish(void) {
    if (atomic_load(&s_mode) != 2) return ESP_ERR_INVALID_STATE;
    int32_t tail[FRAMES * 2];
    for (size_t i = 0; i < FRAMES; ++i) {
        int16_t value = (int16_t)((int32_t)s_last * (int32_t)(FRAMES - 1 - i) / FRAMES);
        tail[i * 2] = tail[i * 2 + 1] = tars_audio_pcm16_to_slot(value, s_volume);
    }
    esp_err_t err = write_slots(tail, FRAMES);
    memset(tail, 0, sizeof(tail));
    for (int i = 0; i < DMA_COUNT && err == ESP_OK; ++i) err = write_slots(tail, FRAMES);
    if (err == ESP_OK) vTaskDelay(pdMS_TO_TICKS(40) + 1); /* >32ms DMA depth at 16kHz */
    (void)tars_audio_amp_set_enabled(false);
    i2s_channel_disable(s_tx);
    atomic_store(&s_mode, 0);
    xSemaphoreGive(s_lock);
    return err;
}

esp_err_t tars_audio_run_self_test(const tars_audio_self_test_config_t *cfg,
                                  tars_audio_self_test_result_t *result) {
    if (!cfg || !result || cfg->mic_duration_ms < 500 || cfg->mic_duration_ms > 3000 ||
        cfg->tone_duration_ms < 200 || cfg->tone_duration_ms > 1000 ||
        cfg->tone_frequency_hz < 200 || cfg->tone_frequency_hz > 2000 ||
        cfg->tone_volume_pct > 5) return ESP_ERR_INVALID_ARG;
    memset(result, 0, sizeof(*result));
    result->sample_rate_hz = TARS_AUDIO_SAMPLE_RATE_HZ;
    esp_err_t err = tars_audio_capture_start();
    if (err != ESP_OK) return err;
    int16_t pcm[FRAMES];
    uint32_t target = (uint32_t)cfg->mic_duration_ms * TARS_AUDIO_SAMPLE_RATE_HZ / 1000;
    int64_t sum = 0; uint64_t squares = 0;
    while (result->mic_samples < target && err == ESP_OK) {
        size_t count = 0;
        err = tars_audio_capture_read(pcm, FRAMES, &count);
        for (size_t i = 0; err == ESP_OK && i < count && result->mic_samples < target; ++i) {
            int32_t x = pcm[i], magnitude = x < 0 ? -x : x;
            sum += x; squares += (int64_t)x * x;
            if ((uint32_t)magnitude > result->mic_peak) result->mic_peak = magnitude;
            if (magnitude >= 32700) ++result->mic_clipped_samples;
            ++result->mic_samples;
        }
    }
    tars_audio_capture_stop();
    if (err != ESP_OK) return err;
    result->mic_dc = sum / result->mic_samples;
    result->mic_rms = sqrt((double)squares / result->mic_samples);
    result->mic_signal_detected = result->mic_peak >= 32;
    err = tars_audio_play_start(cfg->tone_volume_pct);
    if (err != ESP_OK) return err;
    uint32_t total = (uint32_t)cfg->tone_duration_ms * TARS_AUDIO_SAMPLE_RATE_HZ / 1000;
    for (uint32_t offset = 0; offset < total && err == ESP_OK; offset += FRAMES) {
        size_t count = total - offset > FRAMES ? FRAMES : total - offset;
        for (size_t i = 0; i < count; ++i)
            pcm[i] = (int16_t)(sinf(6.283185307f * cfg->tone_frequency_hz * (offset + i) / TARS_AUDIO_SAMPLE_RATE_HZ) * 16000);
        err = tars_audio_play_write(pcm, count);
    }
    esp_err_t finish = tars_audio_play_finish();
    if (err == ESP_OK) err = finish;
    result->tone_played = err == ESP_OK;
    return err;
}
