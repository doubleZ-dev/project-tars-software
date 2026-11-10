#include "tars_usb.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "driver/usb_serial_jtag.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#define FRAME_SIZE 544
typedef struct { uint32_t epoch; char text[FRAME_SIZE]; } tx_frame_t;
static QueueHandle_t s_protocol_queue;
static QueueHandle_t s_log_queue;
static volatile bool s_failed;
static volatile uint32_t s_epoch;

static int framed_log(const char *format, va_list args) {
    tx_frame_t frame = {0};
    int count = vsnprintf(frame.text + sizeof("\n" TARS_USB_LOG_PREFIX) - 1,
                          sizeof(frame.text) - sizeof("\n" TARS_USB_LOG_PREFIX) - 1,
                          format, args);
    memcpy(frame.text, "\n" TARS_USB_LOG_PREFIX, sizeof("\n" TARS_USB_LOG_PREFIX) - 1);
    for (char *p = frame.text + 1; *p; ++p)
        if (*p == '\n' || *p == '\r') *p = ' ';
    size_t length = strlen(frame.text);
    frame.text[length] = '\n';
    frame.text[length + 1] = 0;
    /* Logging cannot wait for USB or block the safety tasks. */
    if (s_log_queue) (void)xQueueSend(s_log_queue, &frame, 0);
    return count;
}

static void tx_task(void *arg) {
    (void)arg;
    tx_frame_t frame;
    while (true) {
        bool protocol = xQueueReceive(s_protocol_queue, &frame, 0) == pdTRUE;
        if (!protocol && xQueueReceive(s_log_queue, &frame, 0) != pdTRUE) {
            /* 5 ms truncates to zero at the default 100 Hz tick rate.
               A zero-delay priority-5 loop starves app_main and IDLE0. */
            vTaskDelay(pdMS_TO_TICKS(5) + 1);
            continue;
        }
        if (protocol && frame.epoch != s_epoch) continue;
        if (!usb_serial_jtag_is_connected()) {
            if (protocol) s_failed = true;
            continue;
        }
        size_t size = strlen(frame.text);
        int written = usb_serial_jtag_write_bytes(frame.text, size, pdMS_TO_TICKS(100));
        if (written != size) s_failed = true;
        /* Every next frame begins with newline: a partial frame cannot absorb it. */
    }
}

esp_err_t tars_usb_init(void) {
    s_protocol_queue = xQueueCreate(16, sizeof(tx_frame_t));
    s_log_queue = xQueueCreate(16, sizeof(tx_frame_t));
    if (!s_protocol_queue || !s_log_queue) return ESP_ERR_NO_MEM;
    usb_serial_jtag_driver_config_t config = {
        .tx_buffer_size = 2048, .rx_buffer_size = 2048,
    };
    esp_err_t err = usb_serial_jtag_driver_install(&config);
    if (err != ESP_OK) return err;
    if (xTaskCreate(tx_task, "usb_mux_tx", 4096, NULL, 5, NULL) != pdPASS)
        return ESP_ERR_NO_MEM;
    /* Application ESP_LOG output has no other USB writer. Early ROM/startup
       text precedes this hook and is explicitly ignored by the PC demux. */
    esp_log_set_vprintf(framed_log);
    return ESP_OK;
}

bool tars_usb_send_protocol(const char *json) {
    tx_frame_t frame = {.epoch = s_epoch};
    int length = snprintf(frame.text, sizeof(frame.text), "\n" TARS_USB_PROTOCOL_PREFIX "%s\n", json);
    if (length < 0 || length >= sizeof(frame.text) ||
        xQueueSend(s_protocol_queue, &frame, 0) != pdTRUE) {
        s_failed = true;
        return false;
    }
    return true;
}

int tars_usb_read(void *data, size_t size) {
    return usb_serial_jtag_read_bytes(data, size, pdMS_TO_TICKS(50));
}

bool tars_usb_link_ok(void) {
    /* SOF indicates host presence, not whether a terminal is open. The
       protocol heartbeat lease separately detects application disappearance. */
    return usb_serial_jtag_is_connected() && !s_failed;
}

void tars_usb_begin_session(void) {
    ++s_epoch;
    xQueueReset(s_protocol_queue);
    s_failed = false;
}
