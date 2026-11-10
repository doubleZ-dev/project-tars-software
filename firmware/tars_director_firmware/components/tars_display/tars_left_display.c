#include "tars_left_display.h"
#include "lvgl.h"
#include <stdio.h>
#include <string.h>

extern const lv_font_t font_5x7;

#define VIEW_Y 54

#define COLOR_BG       0x000000
#define COLOR_TEXT     0xC7D1C8
#define COLOR_MUTED    0x526158
#define COLOR_GRID     0x26312B
#define COLOR_BODY     0xE29C38
#define COLOR_ACTIVE   0x6FA77C
#define COLOR_WARN     0xD6A44A
#define COLOR_FAULT    0xD15B45

static tars_left_state_t g_state;
static lv_obj_t *g_status;
static lv_obj_t *g_power;
static lv_obj_t *g_voice;
static lv_obj_t *g_mode;
static lv_obj_t *g_task;
static lv_obj_t *g_link;
static lv_obj_t *g_traits;
static lv_obj_t *g_left_leg;
static lv_obj_t *g_right_leg;
static lv_obj_t *g_sensor_left;
static lv_obj_t *g_sensor_right;
static lv_obj_t *g_name_letters[4];
static lv_timer_t *g_anim_timer;
static uint16_t g_step;
static uint8_t g_anim_frame;

static lv_obj_t *make_rect(lv_obj_t *parent, int x, int y, int w, int h, uint32_t color)
{
    lv_obj_t *obj = lv_obj_create(parent);
    lv_obj_set_pos(obj, x, y);
    lv_obj_set_size(obj, w, h);
    lv_obj_set_style_bg_color(obj, lv_color_hex(color), 0);
    lv_obj_set_style_bg_opa(obj, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(obj, 0, 0);
    lv_obj_set_style_radius(obj, 0, 0);
    lv_obj_set_style_pad_all(obj, 0, 0);
    lv_obj_remove_flag(obj, LV_OBJ_FLAG_SCROLLABLE);
    return obj;
}

static lv_obj_t *make_transparent_group(lv_obj_t *parent, int x, int y, int w, int h)
{
    lv_obj_t *group = lv_obj_create(parent);
    lv_obj_set_pos(group, x, y);
    lv_obj_set_size(group, w, h);
    lv_obj_set_style_bg_opa(group, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(group, 0, 0);
    lv_obj_set_style_radius(group, 0, 0);
    lv_obj_set_style_pad_all(group, 0, 0);
    lv_obj_remove_flag(group, LV_OBJ_FLAG_SCROLLABLE);
    return group;
}

static lv_obj_t *make_label(lv_obj_t *parent, int x, int y, uint32_t color)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_obj_set_pos(label, x, y);
    lv_obj_set_style_text_font(label, &font_5x7, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(color), 0);
    lv_obj_set_style_text_letter_space(label, 0, 0);
    return label;
}

static lv_obj_t *make_compact_label(lv_obj_t *parent, int x, int y, uint32_t color)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_obj_set_pos(label, x, y);
    lv_obj_set_style_text_font(label, &lv_font_montserrat_8, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(color), 0);
    lv_obj_set_style_text_letter_space(label, 0, 0);
    return label;
}

static void set_label_color(lv_obj_t *label, uint32_t color)
{
    lv_obj_set_style_text_color(label, lv_color_hex(color), 0);
}

static void set_sensor_color(uint32_t left, uint32_t right)
{
    lv_obj_set_style_bg_color(g_sensor_left, lv_color_hex(left), 0);
    lv_obj_set_style_bg_color(g_sensor_right, lv_color_hex(right), 0);
}

static void create_leg_segments(lv_obj_t *leg)
{
    make_rect(leg, 0, 0, 15, 19, COLOR_BODY);
    make_rect(leg, 0, 21, 15, 20, COLOR_BODY);
    make_rect(leg, 0, 43, 15, 20, COLOR_BODY);
}

static void create_tars_figure(lv_obj_t *parent)
{
    const int top = VIEW_Y + 15;

    g_left_leg = make_transparent_group(parent, 7, top, 15, 65);
    g_right_leg = make_transparent_group(parent, 58, top, 15, 65);
    create_leg_segments(g_left_leg);
    create_leg_segments(g_right_leg);

    /* Two fixed center columns. */
    make_rect(parent, 24, top, 15, 19, COLOR_BODY);
    make_rect(parent, 24, top + 21, 15, 27, COLOR_BODY);
    make_rect(parent, 24, top + 50, 15, 13, COLOR_BODY);
    make_rect(parent, 41, top, 15, 19, COLOR_BODY);
    make_rect(parent, 41, top + 21, 15, 27, COLOR_BODY);
    make_rect(parent, 41, top + 50, 15, 13, COLOR_BODY);

    /* Upper bridge and twin display apertures from the physical TARS body. */
    make_rect(parent, 7, top, 66, 7, COLOR_BODY);
    make_rect(parent, 22, top + 9, 36, 13, COLOR_BODY);
    make_rect(parent, 24, top + 11, 14, 9, COLOR_BG);
    make_rect(parent, 42, top + 11, 14, 9, COLOR_BG);
    g_sensor_left = make_rect(parent, 27, top + 13, 8, 5, COLOR_GRID);
    g_sensor_right = make_rect(parent, 45, top + 13, 8, 5, COLOR_ACTIVE);

    /* Vertical nameplate. Four separate glyphs guarantee that S is visible. */
    const char *letters[] = {"T", "A", "R", "S"};
    for (int i = 0; i < 4; i++) {
        g_name_letters[i] = make_compact_label(parent, 28, top + 22 + i * 6, COLOR_BG);
        lv_label_set_text(g_name_letters[i], letters[i]);
    }

    /* Lower center opening. */
    make_rect(parent, 31, top + 56, 18, 5, COLOR_BG);
}

static void anim_tick_cb(lv_timer_t *timer)
{
    (void)timer;
    g_anim_frame = (g_anim_frame + 1) & 3;

    if (g_state == TARS_LEFT_WALKING) {
        static const int8_t left_y[4] = {2, 0, -2, 0};
        static const int8_t right_y[4] = {-2, 0, 2, 0};
        lv_obj_set_y(g_left_leg, VIEW_Y + 15 + left_y[g_anim_frame]);
        lv_obj_set_y(g_right_leg, VIEW_Y + 15 + right_y[g_anim_frame]);
        set_sensor_color(COLOR_ACTIVE, COLOR_ACTIVE);
    } else {
        lv_obj_set_y(g_left_leg, VIEW_Y + 15);
        lv_obj_set_y(g_right_leg, VIEW_Y + 15);

        if (g_state == TARS_LEFT_LISTENING) {
            uint32_t pulse = (g_anim_frame & 1) ? COLOR_ACTIVE : COLOR_GRID;
            set_sensor_color(pulse, pulse);
        } else if (g_state == TARS_LEFT_ANALYZING) {
            set_sensor_color((g_anim_frame & 1) ? COLOR_ACTIVE : COLOR_GRID,
                             (g_anim_frame & 1) ? COLOR_GRID : COLOR_ACTIVE);
        }
    }
}

void tars_left_display_init(lv_obj_t *parent)
{
    lv_obj_set_style_bg_color(parent, lv_color_hex(COLOR_BG), 0);
    lv_obj_set_style_bg_opa(parent, LV_OPA_COVER, 0);

    lv_obj_t *title = make_label(parent, 4, VIEW_Y + 2, COLOR_ACTIVE);
    lv_label_set_text(title, "TARS/03");
    g_status = make_label(parent, 52, VIEW_Y + 2, COLOR_ACTIVE);
    g_power = make_label(parent, 92, VIEW_Y + 2, COLOR_TEXT);
    lv_label_set_text(g_power, "P87%");
    make_rect(parent, 4, VIEW_Y + 12, 120, 1, COLOR_GRID);

    create_tars_figure(parent);

    g_voice = make_label(parent, 74, VIEW_Y + 16, COLOR_TEXT);
    g_mode = make_label(parent, 74, VIEW_Y + 28, COLOR_TEXT);
    g_task = make_label(parent, 74, VIEW_Y + 40, COLOR_TEXT);
    g_link = make_label(parent, 74, VIEW_Y + 52, COLOR_ACTIVE);

    make_rect(parent, 4, VIEW_Y + 94, 120, 1, COLOR_GRID);
    g_traits = lv_label_create(parent);
    lv_obj_set_pos(g_traits, 4, VIEW_Y + 96);
    lv_obj_set_style_text_font(g_traits, &font_5x7, 0);
    lv_obj_set_style_text_color(g_traits, lv_color_hex(COLOR_TEXT), 0);
    lv_obj_set_style_text_letter_space(g_traits, 0, 0);
    lv_label_set_text(g_traits, "HON 80 HUM 80 TRU 80");

    g_step = 127;
    g_anim_frame = 0;
    g_state = TARS_LEFT_IDLE;
    tars_left_display_set_state(TARS_LEFT_IDLE);
    g_anim_timer = lv_timer_create(anim_tick_cb, 170, NULL);
}

void tars_left_display_set_state(tars_left_state_t state)
{
    g_state = state;
    uint32_t status_color = COLOR_ACTIVE;

    switch (state) {
    case TARS_LEFT_IDLE:
        lv_label_set_text(g_status, "READY");
        lv_label_set_text(g_voice, "VOICE STBY");
        lv_label_set_text(g_mode, "MODE  IDLE");
        lv_label_set_text(g_link, "LINK  OK");
        set_sensor_color(COLOR_GRID, COLOR_ACTIVE);
        break;
    case TARS_LEFT_LISTENING:
        lv_label_set_text(g_status, "LISTN");
        lv_label_set_text(g_voice, "VOICE LIVE");
        lv_label_set_text(g_mode, "MODE DIALOG");
        lv_label_set_text(g_link, "LINK  OK");
        break;
    case TARS_LEFT_ANALYZING:
        lv_label_set_text(g_status, "ANLYZ");
        lv_label_set_text(g_voice, "VOICE DONE");
        lv_label_set_text(g_mode, "MODE DIALOG");
        lv_label_set_text(g_link, "LINK  OK");
        break;
    case TARS_LEFT_WALKING:
        lv_label_set_text(g_status, "MOTION");
        lv_label_set_text(g_voice, "VOICE STBY");
        lv_label_set_text(g_mode, "MODE  WALK");
        lv_label_set_text(g_link, "LINK  OK");
        break;
    case TARS_LEFT_ERROR:
        status_color = COLOR_FAULT;
        lv_label_set_text(g_status, "FAULT");
        lv_label_set_text(g_voice, "VOICE OFF");
        lv_label_set_text(g_mode, "MODE  HOLD");
        lv_label_set_text(g_link, "LINK CHECK");
        set_sensor_color(COLOR_FAULT, COLOR_GRID);
        break;
    }

    lv_label_set_text_fmt(g_task, "TASK  %04u", g_step);
    set_label_color(g_status, status_color);
    set_label_color(g_link, state == TARS_LEFT_ERROR ? COLOR_WARN : COLOR_ACTIVE);
}

void tars_left_display_update_step(uint16_t step)
{
    g_step = step;
    lv_label_set_text_fmt(g_task, "TASK  %04u", g_step);
}

void tars_left_display_update_gait_phase(const char *phase)
{
    (void)phase;
}

void tars_left_display_update_error(const char *module, int recoverable)
{
    if (g_state != TARS_LEFT_ERROR) return;
    lv_label_set_text_fmt(g_mode, "MODE %.5s", module ? module : "HOLD");
    set_label_color(g_link, recoverable ? COLOR_WARN : COLOR_FAULT);
}
