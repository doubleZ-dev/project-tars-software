#pragma once
#include "lvgl.h"

void tars_terminal_init(lv_obj_t *parent);
void tars_terminal_boot_sequence(uint16_t project_day);
void tars_terminal_set_status(const char *mode, const char *detail);
void tars_terminal_print_line(const char *line);
void tars_terminal_type_line(const char *line);
void tars_terminal_append_dialogue(const char *role, const char *text);
void tars_terminal_set_footer(uint16_t day, uint8_t trust);
int tars_terminal_boot_is_done(void);
