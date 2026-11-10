#include "tars_terminal_right.h"
#include "font_5x7.h"
#include "lvgl.h"
#include <stdio.h>
#include <string.h>

#define VIEW_Y       54
#define DIALOG_ROWS   8
#define COLS         21
#define QUEUE_MAX    16
#define TYPE_PERIOD  55
#define DIALOGUE_TEXT_BYTES    304
#define DIALOGUE_WIDTH         120
#define DIALOGUE_HEIGHT        75
#define DIALOGUE_GAP           2
#define DIALOGUE_TYPE_PERIOD   45

#define COLOR_BG       0x000000
#define COLOR_TEXT     0xC7D1C8
#define COLOR_MUTED    0x526158
#define COLOR_GRID     0x26312B
#define COLOR_ACTIVE   0x6FA77C
#define COLOR_USER     0x526158

static lv_obj_t *g_header;
static lv_obj_t *g_rows[DIALOG_ROWS];
static lv_obj_t *g_footer;
static lv_obj_t *g_cursor;
static lv_obj_t *g_dialog_view;
static lv_obj_t *g_user_dialog;
static lv_obj_t *g_tars_dialog;
static char g_text[DIALOG_ROWS][COLS];
static char g_queue[QUEUE_MAX][COLS];
static char g_current[COLS];
static lv_timer_t *g_type_timer;
static uint8_t g_queue_head;
static uint8_t g_queue_tail;
static uint8_t g_queue_count;
static uint8_t g_type_pos;
static uint8_t g_type_row;
static uint8_t g_boot_active;
static int g_boot_done;
static uint8_t g_dialog_active;
static char g_user_message[DIALOGUE_TEXT_BYTES];
static char g_tars_message[DIALOGUE_TEXT_BYTES];
static char g_user_visible[DIALOGUE_TEXT_BYTES];
static char g_tars_visible[DIALOGUE_TEXT_BYTES];
static lv_timer_t *g_dialog_type_timer;
static const char *g_dialog_type_source;
static char *g_dialog_type_target;
static size_t g_dialog_type_bytes;

extern const lv_font_t tars_font_cjk_12;

static lv_obj_t *make_label(lv_obj_t *parent, int x, int y, uint32_t color)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_obj_set_pos(label, x, y);
    lv_obj_set_style_text_font(label, &font_5x7, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(color), 0);
    lv_obj_set_style_text_letter_space(label, 0, 0);
    return label;
}

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

static void refresh_rows(void)
{
    for (int i = 0; i < DIALOG_ROWS; i++) {
        lv_label_set_text(g_rows[i], g_text[i]);
        uint32_t color = strncmp(g_text[i], "USER:", 5) == 0 ? COLOR_USER : COLOR_ACTIVE;
        lv_obj_set_style_text_color(g_rows[i], lv_color_hex(color), 0);
    }
}

static uint8_t reserve_row(void)
{
    for (int i = 0; i < DIALOG_ROWS; i++) {
        if (g_text[i][0] == '\0') return i;
    }

    for (int i = 0; i < DIALOG_ROWS - 1; i++)
        strcpy(g_text[i], g_text[i + 1]);
    g_text[DIALOG_ROWS - 1][0] = '\0';
    refresh_rows();
    return DIALOG_ROWS - 1;
}

static void show_cursor(void)
{
    lv_obj_set_pos(g_cursor, 4 + g_type_pos * 6, VIEW_Y + 16 + g_type_row * 9);
    lv_obj_remove_flag(g_cursor, LV_OBJ_FLAG_HIDDEN);
}

static void hide_cursor(void)
{
    lv_obj_add_flag(g_cursor, LV_OBJ_FLAG_HIDDEN);
}

static void show_dialogue_view(void)
{
    if (!g_dialog_active) {
        for (int i = 0; i < DIALOG_ROWS; i++)
            lv_obj_add_flag(g_rows[i], LV_OBJ_FLAG_HIDDEN);
        hide_cursor();
        lv_obj_remove_flag(g_dialog_view, LV_OBJ_FLAG_HIDDEN);
        g_dialog_active = 1;
    }
}

static size_t utf8_sequence_length(const char *text)
{
    unsigned char first = (unsigned char)text[0];
    size_t remaining = strlen(text);
    size_t length = 1;
    if ((first & 0xE0) == 0xC0) length = 2;
    else if ((first & 0xF0) == 0xE0) length = 3;
    else if ((first & 0xF8) == 0xF0) length = 4;
    return length <= remaining ? length : 1;
}

static int32_t prepare_dialogue_label(lv_obj_t *label, char *text)
{
    if (!text[0]) {
        lv_obj_add_flag(label, LV_OBJ_FLAG_HIDDEN);
        return 0;
    }
    lv_obj_remove_flag(label, LV_OBJ_FLAG_HIDDEN);
    lv_label_set_text_static(label, text);
    lv_obj_set_size(label, DIALOGUE_WIDTH, LV_SIZE_CONTENT);
    return lv_obj_get_height(label);
}

static void update_dialogue_layout(void)
{
    int32_t user_height = prepare_dialogue_label(g_user_dialog, g_user_visible);
    int32_t tars_height = prepare_dialogue_label(g_tars_dialog, g_tars_visible);
    lv_obj_update_layout(g_dialog_view);
    if (user_height) user_height = lv_obj_get_height(g_user_dialog);
    if (tars_height) tars_height = lv_obj_get_height(g_tars_dialog);

    int32_t gap = user_height && tars_height ? DIALOGUE_GAP : 0;
    int32_t content_height = user_height + gap + tars_height;
    int32_t scroll = content_height > DIALOGUE_HEIGHT ? content_height - DIALOGUE_HEIGHT : 0;
    if (user_height) lv_obj_set_pos(g_user_dialog, 0, -scroll);
    if (tars_height) lv_obj_set_pos(g_tars_dialog, 0, user_height + gap - scroll);
}

static void stop_dialogue_typing(int complete)
{
    if (g_dialog_type_timer) {
        lv_timer_del(g_dialog_type_timer);
        g_dialog_type_timer = NULL;
    }
    if (complete && g_dialog_type_source && g_dialog_type_target) {
        strncpy(g_dialog_type_target, g_dialog_type_source, DIALOGUE_TEXT_BYTES - 1);
        g_dialog_type_target[DIALOGUE_TEXT_BYTES - 1] = '\0';
    }
    g_dialog_type_source = NULL;
    g_dialog_type_target = NULL;
    g_dialog_type_bytes = 0;
}

static void dialogue_type_tick(lv_timer_t *timer)
{
    (void)timer;
    size_t length = strlen(g_dialog_type_source);
    if (g_dialog_type_bytes >= length) {
        stop_dialogue_typing(0);
        return;
    }

    size_t sequence = utf8_sequence_length(g_dialog_type_source + g_dialog_type_bytes);
    if (g_dialog_type_bytes + sequence >= DIALOGUE_TEXT_BYTES)
        sequence = DIALOGUE_TEXT_BYTES - g_dialog_type_bytes - 1;
    memcpy(g_dialog_type_target + g_dialog_type_bytes,
           g_dialog_type_source + g_dialog_type_bytes, sequence);
    g_dialog_type_bytes += sequence;
    g_dialog_type_target[g_dialog_type_bytes] = '\0';
    update_dialogue_layout();
}

static void start_dialogue_typing(const char *source, char *target)
{
    stop_dialogue_typing(0);
    size_t prefix = strlen(source) >= 2 ? 2 : strlen(source);
    memcpy(target, source, prefix);
    target[prefix] = '\0';
    g_dialog_type_source = source;
    g_dialog_type_target = target;
    g_dialog_type_bytes = prefix;
    update_dialogue_layout();
    if (source[prefix])
        g_dialog_type_timer = lv_timer_create(dialogue_type_tick,
                                              DIALOGUE_TYPE_PERIOD, NULL);
}

void tars_terminal_append_dialogue(const char *role, const char *text)
{
    if (!role || !text || !text[0]) return;

    show_dialogue_view();
    if (strcmp(role, "user") == 0) {
        stop_dialogue_typing(0);
        snprintf(g_user_message, sizeof(g_user_message), "U:%s", text);
        g_tars_message[0] = '\0';
        g_tars_visible[0] = '\0';
        start_dialogue_typing(g_user_message, g_user_visible);
    } else {
        stop_dialogue_typing(1);
        update_dialogue_layout();
        snprintf(g_tars_message, sizeof(g_tars_message), "T:%s", text);
        start_dialogue_typing(g_tars_message, g_tars_visible);
    }
}

static void finish_boot_if_ready(void)
{
    if (g_boot_active && g_queue_count == 0 && g_type_timer == NULL) {
        g_boot_active = 0;
        g_boot_done = 1;
        lv_label_set_text(g_header, "DIALOG/04 READY");
        lv_label_set_text(g_footer, "READY       LINK OK");
    }
}

static void start_next_typed(void);

static void type_tick(lv_timer_t *timer)
{
    (void)timer;
    size_t len = strlen(g_current);
    if (g_type_pos < len) {
        g_text[g_type_row][g_type_pos] = g_current[g_type_pos];
        g_type_pos++;
        g_text[g_type_row][g_type_pos] = '\0';
        lv_label_set_text(g_rows[g_type_row], g_text[g_type_row]);
        show_cursor();
        return;
    }

    hide_cursor();
    lv_timer_del(g_type_timer);
    g_type_timer = NULL;
    if (g_queue_count > 0) start_next_typed();
    finish_boot_if_ready();
}

static void start_next_typed(void)
{
    if (g_queue_count == 0 || g_type_timer != NULL) return;

    strncpy(g_current, g_queue[g_queue_head], COLS - 1);
    g_current[COLS - 1] = '\0';
    g_queue_head = (g_queue_head + 1) % QUEUE_MAX;
    g_queue_count--;

    g_type_row = reserve_row();
    g_text[g_type_row][0] = '\0';
    g_type_pos = 0;
    lv_label_set_text(g_rows[g_type_row], "");
    lv_obj_set_style_text_color(g_rows[g_type_row], lv_color_hex(COLOR_ACTIVE), 0);
    show_cursor();
    g_type_timer = lv_timer_create(type_tick, TYPE_PERIOD, NULL);
}

static void enqueue_typed(const char *line)
{
    if (!line || g_queue_count >= QUEUE_MAX) return;
    strncpy(g_queue[g_queue_tail], line, COLS - 1);
    g_queue[g_queue_tail][COLS - 1] = '\0';
    g_queue_tail = (g_queue_tail + 1) % QUEUE_MAX;
    g_queue_count++;
    start_next_typed();
}

static void append_immediate(const char *line)
{
    uint8_t row = reserve_row();
    strncpy(g_text[row], line ? line : "", COLS - 1);
    g_text[row][COLS - 1] = '\0';
    refresh_rows();
}

void tars_terminal_init(lv_obj_t *parent)
{
    lv_obj_set_style_bg_color(parent, lv_color_hex(COLOR_BG), 0);
    lv_obj_set_style_bg_opa(parent, LV_OPA_COVER, 0);

    g_header = make_label(parent, 4, VIEW_Y + 2, COLOR_ACTIVE);
    lv_label_set_text(g_header, "DIALOG/04 BOOT");
    make_rect(parent, 4, VIEW_Y + 12, 120, 1, COLOR_GRID);

    for (int i = 0; i < DIALOG_ROWS; i++) {
        g_rows[i] = make_label(parent, 4, VIEW_Y + 16 + i * 9, COLOR_ACTIVE);
        g_text[i][0] = '\0';
        lv_label_set_text(g_rows[i], "");
    }

    g_dialog_view = make_rect(parent, 4, VIEW_Y + 16, DIALOGUE_WIDTH, DIALOGUE_HEIGHT, COLOR_BG);
    lv_obj_set_style_bg_opa(g_dialog_view, LV_OPA_TRANSP, 0);
    lv_obj_remove_flag(g_dialog_view, LV_OBJ_FLAG_OVERFLOW_VISIBLE);

    g_user_dialog = lv_label_create(g_dialog_view);
    lv_obj_set_style_text_font(g_user_dialog, &tars_font_cjk_12, 0);
    lv_obj_set_style_text_color(g_user_dialog, lv_color_hex(COLOR_TEXT), 0);
    lv_obj_set_style_text_letter_space(g_user_dialog, 0, 0);
    lv_obj_set_style_text_line_space(g_user_dialog, 0, 0);
    lv_label_set_long_mode(g_user_dialog, LV_LABEL_LONG_WRAP);
    lv_obj_set_size(g_user_dialog, DIALOGUE_WIDTH, LV_SIZE_CONTENT);
    lv_label_set_text(g_user_dialog, "");
    lv_obj_add_flag(g_user_dialog, LV_OBJ_FLAG_HIDDEN);

    g_tars_dialog = lv_label_create(g_dialog_view);
    lv_obj_set_style_text_font(g_tars_dialog, &tars_font_cjk_12, 0);
    lv_obj_set_style_text_color(g_tars_dialog, lv_color_hex(COLOR_ACTIVE), 0);
    lv_obj_set_style_text_letter_space(g_tars_dialog, 0, 0);
    lv_obj_set_style_text_line_space(g_tars_dialog, 0, 0);
    lv_label_set_long_mode(g_tars_dialog, LV_LABEL_LONG_WRAP);
    lv_obj_set_size(g_tars_dialog, DIALOGUE_WIDTH, LV_SIZE_CONTENT);
    lv_label_set_text(g_tars_dialog, "");
    lv_obj_add_flag(g_tars_dialog, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(g_dialog_view, LV_OBJ_FLAG_HIDDEN);

    make_rect(parent, 4, VIEW_Y + 91, 120, 1, COLOR_GRID);
    g_footer = make_label(parent, 4, VIEW_Y + 95, COLOR_ACTIVE);
    lv_label_set_text(g_footer, "BOOTING     LINK OK");
    g_cursor = make_rect(parent, 4, VIEW_Y + 16, 3, 7, COLOR_ACTIVE);
    hide_cursor();

    g_type_timer = NULL;
    g_queue_head = 0;
    g_queue_tail = 0;
    g_queue_count = 0;
    g_boot_active = 0;
    g_boot_done = 0;
    g_dialog_active = 0;
    g_user_message[0] = '\0';
    g_tars_message[0] = '\0';
    g_user_visible[0] = '\0';
    g_tars_visible[0] = '\0';
    g_dialog_type_timer = NULL;
    g_dialog_type_source = NULL;
    g_dialog_type_target = NULL;
    g_dialog_type_bytes = 0;
}

void tars_terminal_boot_sequence(uint16_t project_day)
{
    (void)project_day;
    if (g_type_timer) {
        lv_timer_del(g_type_timer);
        g_type_timer = NULL;
    }
    for (int i = 0; i < DIALOG_ROWS; i++) g_text[i][0] = '\0';
    for (int i = 0; i < DIALOG_ROWS; i++)
        lv_obj_remove_flag(g_rows[i], LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(g_dialog_view, LV_OBJ_FLAG_HIDDEN);
    stop_dialogue_typing(0);
    g_user_message[0] = '\0';
    g_tars_message[0] = '\0';
    g_user_visible[0] = '\0';
    g_tars_visible[0] = '\0';
    update_dialogue_layout();
    g_dialog_active = 0;
    refresh_rows();
    g_queue_head = g_queue_tail = g_queue_count = 0;
    g_boot_done = 0;
    g_boot_active = 1;
    lv_label_set_text(g_header, "DIALOG/04 BOOT");
    lv_label_set_text(g_footer, "BOOTING     LINK OK");
    enqueue_typed("TARS: ONLINE");
    enqueue_typed("TARS: I AM READY.");
    enqueue_typed("TARS: AWAITING TASK");
}

void tars_terminal_set_status(const char *mode, const char *detail)
{
    (void)detail;
    char header[COLS];
    char footer[COLS];
    snprintf(header, sizeof(header), "DIALOG/04 %.8s", mode ? mode : "READY");
    snprintf(footer, sizeof(footer), "%-11.11s LINK OK", mode ? mode : "READY");
    lv_label_set_text(g_header, header);
    lv_label_set_text(g_footer, footer);
}

void tars_terminal_print_line(const char *line) { append_immediate(line); }
void tars_terminal_type_line(const char *line) { enqueue_typed(line); }
void tars_terminal_set_footer(uint16_t day, uint8_t trust) { (void)day; (void)trust; }
int tars_terminal_boot_is_done(void) { return g_boot_done; }
