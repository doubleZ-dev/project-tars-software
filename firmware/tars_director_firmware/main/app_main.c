#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "tars_audio.h"
#include "tars_usb.h"
#include "tars_display.h"
#include "tars_motion.h"
#include "tars_protocol.h"
#include "tars_state.h"
#include "tars_diagnostics.h"
#include "tars_wifi_audio.h"
#include "tars_xiaozhi_audio.h"

static void ui_publisher_task(void *arg) {
    (void)arg;
    uint32_t last_sequence = 0;
    while (true) {
        tars_snapshot_t snapshot = tars_state_get();
        if (snapshot.sequence != last_sequence) {
            tars_display_post_snapshot(&snapshot);
            last_sequence = snapshot.sequence;
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

void app_main(void) {
    /* Keep the amplifier safe even if USB allocation/installation fails. */
    ESP_ERROR_CHECK(tars_audio_init());
    ESP_ERROR_CHECK(tars_audio_prepare());
    tars_diag_init();
    ESP_ERROR_CHECK(tars_usb_init());
    tars_diag_log_boot();
    tars_state_init();
    tars_motion_init();
    tars_display_init();
    ESP_ERROR_CHECK(tars_protocol_init());
    esp_err_t wifi_err = tars_wifi_audio_init();
    if (wifi_err != ESP_OK) ESP_LOGE("tars", "Wi-Fi audio unavailable: %s", esp_err_to_name(wifi_err));
    if(wifi_err==ESP_OK) ESP_ERROR_CHECK(tars_xiaozhi_audio_init());
    xTaskCreate(ui_publisher_task, "ui_publisher", 3072, NULL, 3, NULL);
    ESP_LOGI("tars", "TARS PCB V1 Stage 4: disarmed, USB control/debug; optional LAN audio");
}
