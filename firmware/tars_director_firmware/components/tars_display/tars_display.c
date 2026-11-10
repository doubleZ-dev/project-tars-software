/*
 * TARS Display Demo — Dual Screen UI Test
 *
 * PCB H1 (LCD1): Terminal Display (128×160).
 * PCB H4 (LCD2): AI/Status Display (128×106 visible).
 * Physical left/right placement must be verified on the PCB assembly.
 *
 * Hardware init kept identical to spi_lcd_touch_example_main.c.
 * SPI/LVGL init NOT rewritten — follows existing working code.
 */

#include <stdio.h>
#include <stdatomic.h>
#include <string.h>
#include <unistd.h>
#include <sys/lock.h>
#include <sys/param.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_panel_ops.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_err.h"
#include "esp_log.h"
#include "lvgl.h"

#include "esp_lcd_st7735.h"

#include "tars_left_display.h"
#include "tars_terminal_right.h"
#include "tars_ui_state.h"
#include "tars_display.h"
#include "tars_board.h"
#include "tars_orbit.h"

static const char *TAG = "tars_display";
static QueueHandle_t s_ui_queue;
static QueueHandle_t s_dialogue_queue;
static unsigned s_dropped_updates;
static lv_obj_t *s_legacy_right, *s_legacy_left;
static atomic_bool s_legacy_requested, s_legacy_applied;
void tars_display_set_legacy(bool legacy) { atomic_store(&s_legacy_requested,legacy); }
bool tars_display_is_legacy(void) { return atomic_load(&s_legacy_applied); }

typedef struct {
    char role[5];
    char text[TARS_DIALOGUE_TEXT_MAX_BYTES + 1];
} dialogue_update_t;

/* Board routing only; UI and panel rendering retain the original logic. */
#define LCD_HOST  SPI2_HOST
#define EXAMPLE_LCD_PIXEL_CLOCK_HZ     (20 * 1000 * 1000)
#define EXAMPLE_LCD_BK_LIGHT_ON_LEVEL  1
#define EXAMPLE_LCD_BK_LIGHT_OFF_LEVEL !EXAMPLE_LCD_BK_LIGHT_ON_LEVEL
#define EXAMPLE_PIN_NUM_SCLK           TARS_BOARD_LCD_SCLK_GPIO
#define EXAMPLE_PIN_NUM_MOSI           TARS_BOARD_LCD_MOSI_GPIO
#define EXAMPLE_PIN_NUM_MISO           TARS_BOARD_LCD_MISO_GPIO
#define EXAMPLE_PIN_NUM_LCD_DC         TARS_BOARD_LCD_DC_GPIO
#define EXAMPLE_PIN_NUM_LCD_RST        TARS_BOARD_LCD_RESET_GPIO
#define EXAMPLE_PIN_NUM_LCD_CS         TARS_BOARD_LCD_TERMINAL_CS_GPIO
#define EXAMPLE_PIN_NUM_LCD_CS2        TARS_BOARD_LCD_STATUS_CS_GPIO
#define EXAMPLE_PIN_NUM_BK_LIGHT       TARS_BOARD_LCD_BACKLIGHT_GPIO

#define EXAMPLE_LCD_H_RES              128
#define EXAMPLE_LCD_V_RES              160

#define EXAMPLE_LCD_CMD_BITS           8
#define EXAMPLE_LCD_PARAM_BITS         8
#define EXAMPLE_LVGL_DRAW_BUF_LINES    20
#define EXAMPLE_LVGL_TICK_PERIOD_MS    2
#define EXAMPLE_LVGL_TASK_MAX_DELAY_MS 500
#define EXAMPLE_LVGL_TASK_MIN_DELAY_MS 1000 / CONFIG_FREERTOS_HZ
#define EXAMPLE_LVGL_TASK_STACK_SIZE   (4 * 1024)
#define EXAMPLE_LVGL_TASK_PRIORITY     2

static _lock_t lvgl_api_lock;

/* ── LVGL callbacks (same as existing) ── */

static bool example_notify_lvgl_flush_ready(esp_lcd_panel_io_handle_t panel_io,
                                             esp_lcd_panel_io_event_data_t *edata,
                                             void *user_ctx)
{
    lv_display_flush_ready((lv_display_t *)user_ctx);
    return false;
}

static void example_lvgl_port_update_callback(lv_display_t *disp)
{
    esp_lcd_panel_handle_t panel_handle = lv_display_get_user_data(disp);
    lv_display_rotation_t rotation = lv_display_get_rotation(disp);
    switch (rotation) {
    case LV_DISPLAY_ROTATION_0:
        esp_lcd_panel_swap_xy(panel_handle, false);
        esp_lcd_panel_mirror(panel_handle, true, false);
        break;
    case LV_DISPLAY_ROTATION_90:
        esp_lcd_panel_swap_xy(panel_handle, true);
        esp_lcd_panel_mirror(panel_handle, true, true);
        break;
    case LV_DISPLAY_ROTATION_180:
        esp_lcd_panel_swap_xy(panel_handle, false);
        esp_lcd_panel_mirror(panel_handle, true, true);
        break;
    case LV_DISPLAY_ROTATION_270:
        esp_lcd_panel_swap_xy(panel_handle, true);
        esp_lcd_panel_mirror(panel_handle, false, false);
        break;
    }
}

static void example_lvgl_flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map)
{
    example_lvgl_port_update_callback(disp);
    esp_lcd_panel_handle_t panel_handle = lv_display_get_user_data(disp);
    int offsetx1 = area->x1;
    int offsetx2 = area->x2;
    int offsety1 = area->y1;
    int offsety2 = area->y2;
    lv_draw_sw_rgb565_swap(px_map, (offsetx2 + 1 - offsetx1) * (offsety2 + 1 - offsety1));
    esp_lcd_panel_draw_bitmap(panel_handle, offsetx1, offsety1, offsetx2 + 1, offsety2 + 1, px_map);
}

static void example_increase_lvgl_tick(void *arg)
{
    lv_tick_inc(EXAMPLE_LVGL_TICK_PERIOD_MS);
}

static void example_lvgl_port_task(void *arg)
{
    ESP_LOGI(TAG, "Starting LVGL task");
    uint32_t time_till_next_ms = 0;
    while (1) {
        _lock_acquire(&lvgl_api_lock);
        time_till_next_ms = lv_timer_handler();
        _lock_release(&lvgl_api_lock);
        time_till_next_ms = MAX(time_till_next_ms, EXAMPLE_LVGL_TASK_MIN_DELAY_MS);
        time_till_next_ms = MIN(time_till_next_ms, EXAMPLE_LVGL_TASK_MAX_DELAY_MS);
        usleep(1000 * time_till_next_ms);
    }
}

static void ui_queue_timer_cb(lv_timer_t *timer)
{
    (void)timer;
    bool legacy=atomic_load(&s_legacy_requested);
    if(legacy!=atomic_load(&s_legacy_applied)) {
        if(legacy) {
            lv_obj_remove_flag(s_legacy_right,LV_OBJ_FLAG_HIDDEN);
            lv_obj_remove_flag(s_legacy_left,LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_add_flag(s_legacy_right,LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(s_legacy_left,LV_OBJ_FLAG_HIDDEN);
        }
        tars_orbit_set_visible(!legacy);
        atomic_store(&s_legacy_applied,legacy);
    }
    tars_snapshot_t snapshot;
    while (xQueueReceive(s_ui_queue, &snapshot, 0) == pdTRUE) {
        orbit_id_t state = ORBIT_IDLE;
        if (snapshot.health == TARS_HEALTH_ESTOP || snapshot.health == TARS_HEALTH_FAULT)
            state = ORBIT_ERROR;
        else if (snapshot.motion == TARS_MOTION_EXECUTING || snapshot.motion == TARS_MOTION_STOPPING)
            state = ORBIT_EXECUTING;
        else if (snapshot.interaction == TARS_INTERACTION_LISTENING) state = ORBIT_LISTENING;
        else if (snapshot.interaction == TARS_INTERACTION_THINKING) state = ORBIT_ANALYZING;
        else if (snapshot.interaction == TARS_INTERACTION_SPEAKING) state = ORBIT_SPEAKING;
        tars_orbit_set_state(state);
        tars_left_state_t visual=TARS_LEFT_IDLE;
        const char *status="READY";
        if(state==ORBIT_ERROR){visual=TARS_LEFT_ERROR;status="FAULT";tars_left_display_update_error("SAFE",0);}
        else if(state==ORBIT_EXECUTING){visual=TARS_LEFT_WALKING;status="MOTION";}
        else if(state==ORBIT_LISTENING){visual=TARS_LEFT_LISTENING;status="LISTEN";}
        else if(state==ORBIT_ANALYZING){visual=TARS_LEFT_ANALYZING;status="ANALYZE";}
        else if(state==ORBIT_SPEAKING)status="SPEAK";
        tars_left_display_set_state(visual);
        tars_terminal_set_status(status,snapshot.connected?"LINK OK":"LOCAL");
    }

    dialogue_update_t dialogue;
    while (xQueueReceive(s_dialogue_queue, &dialogue, 0) == pdTRUE) {
        tars_terminal_append_dialogue(dialogue.role,dialogue.text);
    }
}

bool tars_display_post_snapshot(const tars_snapshot_t *snapshot)
{
    if (!snapshot || !s_ui_queue) return false;
    if (xQueueSend(s_ui_queue, snapshot, 0) == pdTRUE) return true;
    tars_snapshot_t discarded;
    (void)xQueueReceive(s_ui_queue, &discarded, 0);
    s_dropped_updates++;
    return xQueueSend(s_ui_queue, snapshot, 0) == pdTRUE;
}

bool tars_display_post_dialogue(const char *role, const char *text, size_t text_length)
{
    if (!role || !text || !s_dialogue_queue || text_length == 0 ||
        text_length > TARS_DIALOGUE_TEXT_MAX_BYTES) return false;

    dialogue_update_t update = {0};
    snprintf(update.role, sizeof(update.role), "%s", role);
    memcpy(update.text, text, text_length);
    update.text[text_length] = '\0';
    if (xQueueSend(s_dialogue_queue, &update, 0) == pdTRUE) return true;

    dialogue_update_t discarded;
    (void)xQueueReceive(s_dialogue_queue, &discarded, 0);
    s_dropped_updates++;
    return xQueueSend(s_dialogue_queue, &update, 0) == pdTRUE;
}

unsigned tars_display_dropped_updates(void) { return s_dropped_updates; }

/* ── Main ─────────────────────────────────────────────── */

void tars_display_init(void)
{
    /* Backlight */
    gpio_config_t bk_gpio_config = {
#if CONFIG_TARS_LCD_BACKLIGHT_DRIVE
        .mode = GPIO_MODE_OUTPUT,
#else
        .mode = GPIO_MODE_INPUT,
#endif
        .pin_bit_mask = 1ULL << EXAMPLE_PIN_NUM_BK_LIGHT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
    };
#if CONFIG_TARS_LCD_BACKLIGHT_DRIVE
    ESP_ERROR_CHECK(gpio_set_level(EXAMPLE_PIN_NUM_BK_LIGHT, EXAMPLE_LCD_BK_LIGHT_OFF_LEVEL));
#endif
    ESP_ERROR_CHECK(gpio_config(&bk_gpio_config));

    /* SPI bus */
    ESP_LOGI(TAG, "Initialize SPI bus");
    spi_bus_config_t buscfg = {
        .sclk_io_num = EXAMPLE_PIN_NUM_SCLK,
        .mosi_io_num = EXAMPLE_PIN_NUM_MOSI,
        .miso_io_num = EXAMPLE_PIN_NUM_MISO,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = EXAMPLE_LCD_H_RES * 80 * sizeof(uint16_t),
    };
    ESP_ERROR_CHECK(spi_bus_initialize(LCD_HOST, &buscfg, SPI_DMA_CH_AUTO));

    /* ── Screen 1 (Left, CS=GPIO4) ── */
    ESP_LOGI(TAG, "Install panel IO for screen 1");
    esp_lcd_panel_io_handle_t io_handle = NULL;
    esp_lcd_panel_io_spi_config_t io_config = {
        .dc_gpio_num = EXAMPLE_PIN_NUM_LCD_DC,
        .cs_gpio_num = EXAMPLE_PIN_NUM_LCD_CS,
        .pclk_hz = EXAMPLE_LCD_PIXEL_CLOCK_HZ,
        .lcd_cmd_bits = EXAMPLE_LCD_CMD_BITS,
        .lcd_param_bits = EXAMPLE_LCD_PARAM_BITS,
        .spi_mode = 0,
        .trans_queue_depth = 10,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_HOST, &io_config, &io_handle));

    esp_lcd_panel_handle_t panel_handle = NULL;
    esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = EXAMPLE_PIN_NUM_LCD_RST,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
    };
    ESP_LOGI(TAG, "Install ST7735S panel driver for screen 1");
    ESP_ERROR_CHECK(esp_lcd_new_panel_st7735(io_handle, &panel_config, &panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_reset(panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_init(panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_set_gap(panel_handle, 0, 0));
    ESP_ERROR_CHECK(esp_lcd_panel_invert_color(panel_handle, false));
    ESP_ERROR_CHECK(esp_lcd_panel_mirror(panel_handle, false, true));
    ESP_ERROR_CHECK(esp_lcd_panel_swap_xy(panel_handle, false));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel_handle, true));

    /* ── Screen 2 (Right, CS=GPIO6) ── */
    ESP_LOGI(TAG, "Install panel IO for screen 2");
    esp_lcd_panel_io_handle_t io_handle2 = NULL;
    esp_lcd_panel_io_spi_config_t io_config2 = {
        .dc_gpio_num = EXAMPLE_PIN_NUM_LCD_DC,
        .cs_gpio_num = EXAMPLE_PIN_NUM_LCD_CS2,
        .pclk_hz = EXAMPLE_LCD_PIXEL_CLOCK_HZ,
        .lcd_cmd_bits = EXAMPLE_LCD_CMD_BITS,
        .lcd_param_bits = EXAMPLE_LCD_PARAM_BITS,
        .spi_mode = 0,
        .trans_queue_depth = 10,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_HOST, &io_config2, &io_handle2));

    esp_lcd_panel_handle_t panel_handle2 = NULL;
    esp_lcd_panel_dev_config_t panel_config2 = {
        .reset_gpio_num = -1,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
    };
    ESP_LOGI(TAG, "Install ST7735S panel driver for screen 2");
    ESP_ERROR_CHECK(esp_lcd_new_panel_st7735(io_handle2, &panel_config2, &panel_handle2));
    ESP_ERROR_CHECK(esp_lcd_panel_init(panel_handle2));
    ESP_ERROR_CHECK(esp_lcd_panel_set_gap(panel_handle2, 0, 0));
    ESP_ERROR_CHECK(esp_lcd_panel_invert_color(panel_handle2, false));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel_handle2, true));

    /* Only drive BLK once the module's electrical input has been confirmed. */
#if CONFIG_TARS_LCD_BACKLIGHT_DRIVE
    ESP_LOGI(TAG, "Turn on LCD backlight");
    ESP_ERROR_CHECK(gpio_set_level(EXAMPLE_PIN_NUM_BK_LIGHT, EXAMPLE_LCD_BK_LIGHT_ON_LEVEL));
#else
    ESP_LOGW(TAG, "GPIO8 BLK high-Z pending LCD module electrical verification");
#endif

    /* ── LVGL init ── */
    ESP_LOGI(TAG, "Initialize LVGL library");
    lv_init();

    /* Display 1: H1 terminal; physical side pending assembly verification. */
    lv_display_t *display = lv_display_create(EXAMPLE_LCD_H_RES, EXAMPLE_LCD_V_RES);
    lv_disp_set_rotation(display, LV_DISP_ROTATION_180);

    size_t draw_buffer_sz = EXAMPLE_LCD_H_RES * EXAMPLE_LVGL_DRAW_BUF_LINES * sizeof(lv_color16_t);
    void *buf1 = spi_bus_dma_memory_alloc(LCD_HOST, draw_buffer_sz, 0);
    assert(buf1);
    void *buf2 = spi_bus_dma_memory_alloc(LCD_HOST, draw_buffer_sz, 0);
    assert(buf2);
    lv_display_set_buffers(display, buf1, buf2, draw_buffer_sz, LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_user_data(display, panel_handle);
    lv_display_set_color_format(display, LV_COLOR_FORMAT_RGB565);
    lv_display_set_flush_cb(display, example_lvgl_flush_cb);

    /* Display 2: physical left AI/state display, CS=GPIO6. */
    lv_display_t *display2 = lv_display_create(EXAMPLE_LCD_H_RES, EXAMPLE_LCD_V_RES);
    lv_disp_set_rotation(display2, LV_DISP_ROTATION_180);
    void *buf3 = spi_bus_dma_memory_alloc(LCD_HOST, draw_buffer_sz, 0);
    void *buf4 = spi_bus_dma_memory_alloc(LCD_HOST, draw_buffer_sz, 0);
    assert(buf3 && buf4);
    lv_display_set_buffers(display2, buf3, buf4, draw_buffer_sz, LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_user_data(display2, panel_handle2);
    lv_display_set_color_format(display2, LV_COLOR_FORMAT_RGB565);
    lv_display_set_flush_cb(display2, example_lvgl_flush_cb);

    /* LVGL tick timer */
    ESP_LOGI(TAG, "Install LVGL tick timer");
    const esp_timer_create_args_t lvgl_tick_timer_args = {
        .callback = &example_increase_lvgl_tick,
        .name = "lvgl_tick"
    };
    esp_timer_handle_t lvgl_tick_timer = NULL;
    ESP_ERROR_CHECK(esp_timer_create(&lvgl_tick_timer_args, &lvgl_tick_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(lvgl_tick_timer, EXAMPLE_LVGL_TICK_PERIOD_MS * 1000));

    /* Flush ready callbacks */
    const esp_lcd_panel_io_callbacks_t cbs = {
        .on_color_trans_done = example_notify_lvgl_flush_ready,
    };
    ESP_ERROR_CHECK(esp_lcd_panel_io_register_event_callbacks(io_handle, &cbs, display));
    ESP_ERROR_CHECK(esp_lcd_panel_io_register_event_callbacks(io_handle2, &cbs, display2));

    /* LVGL task */
    ESP_LOGI(TAG, "Create LVGL task");
    xTaskCreate(example_lvgl_port_task, "LVGL", EXAMPLE_LVGL_TASK_STACK_SIZE, NULL,
                EXAMPLE_LVGL_TASK_PRIORITY, NULL);

    /* ── TARS UI init ── */
    ESP_LOGI(TAG, "Init TARS UI");

    _lock_acquire(&lvgl_api_lock);

    /* Physical right window, CS=GPIO4: system and event terminal. */
    lv_obj_t *right_scr = lv_display_get_screen_active(display);
    lv_obj_set_style_bg_color(right_scr, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(right_scr, LV_OPA_COVER, 0);


    /* Physical left window, CS=GPIO6: motion and state display. */
    lv_obj_t *left_scr = lv_display_get_screen_active(display2);
    lv_obj_set_style_bg_color(left_scr, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(left_scr, LV_OPA_COVER, 0);
    /* Both UI implementations exist for the lifetime of the display. Switching
     * only changes visibility on the LVGL task; no driver/network reinit. */
    s_legacy_right=lv_obj_create(right_scr);s_legacy_left=lv_obj_create(left_scr);
    lv_obj_t *legacy_roots[]={s_legacy_right,s_legacy_left};
    for(int i=0;i<2;i++) {
        lv_obj_remove_style_all(legacy_roots[i]);lv_obj_set_pos(legacy_roots[i],0,0);
        lv_obj_set_size(legacy_roots[i],128,160);
        lv_obj_set_style_bg_color(legacy_roots[i],lv_color_black(),0);
        lv_obj_set_style_bg_opa(legacy_roots[i],LV_OPA_COVER,0);
        lv_obj_remove_flag(legacy_roots[i],LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(legacy_roots[i],LV_OBJ_FLAG_HIDDEN);
    }
    tars_terminal_init(s_legacy_right);tars_left_display_init(s_legacy_left);
    tars_terminal_boot_sequence(1);
    tars_orbit_init(right_scr, left_scr, false);

    _lock_release(&lvgl_api_lock);

    /* Queue is drained by an LVGL timer, so widget APIs stay on the LVGL task. */
    s_ui_queue = xQueueCreate(8, sizeof(tars_snapshot_t));
    s_dialogue_queue = xQueueCreate(8, sizeof(dialogue_update_t));
    ESP_ERROR_CHECK((s_ui_queue && s_dialogue_queue) ? ESP_OK : ESP_ERR_NO_MEM);
    _lock_acquire(&lvgl_api_lock);
    lv_timer_create(ui_queue_timer_cb, 50, NULL);
    _lock_release(&lvgl_api_lock);

    ESP_LOGI(TAG, "TARS display running; updates are queue-owned");
}
