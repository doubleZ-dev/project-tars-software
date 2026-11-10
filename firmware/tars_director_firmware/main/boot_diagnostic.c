/* Diagnostic build: early raw USB startup/panic output, then framed application
   transport. Do not use as production log-separation acceptance evidence. */
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "tars_audio.h"
#include "tars_state.h"
#include "tars_motion.h"
#include "tars_display.h"
#include "tars_usb.h"
#include "tars_protocol.h"

void app_main(void) {
    ESP_LOGW("boot_diag", "CHECKPOINT 1: app_main reached; no automatic Arm");
    ESP_ERROR_CHECK(tars_audio_init());
    ESP_LOGW("boot_diag", "CHECKPOINT 2: audio muted");
    ESP_LOGW("boot_diag", "CHECKPOINT 2a: entering USB driver init");
    ESP_ERROR_CHECK(tars_usb_init());
    ESP_LOGW("boot_diag", "CHECKPOINT 2b: USB driver init returned");
    tars_state_init();
    tars_motion_init();
    ESP_LOGW("boot_diag", "CHECKPOINT 3: motion initialized DISARMED");
    tars_display_init();
    ESP_LOGW("boot_diag", "CHECKPOINT 4: display initialized");
    ESP_ERROR_CHECK(tars_protocol_init());
    while (true) {
        ESP_LOGW("boot_diag", "ALIVE: diagnostic image; no automatic Arm");
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}
