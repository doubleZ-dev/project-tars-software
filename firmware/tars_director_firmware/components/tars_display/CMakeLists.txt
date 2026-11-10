idf_component_register(
    SRCS "tars_display.c" "tars_left_display.c" "tars_terminal_right.c" "tars_font_cjk_12.c"
    INCLUDE_DIRS "include"
    REQUIRES tars_orbit tars_board tars_core esp_lcd esp_driver_gpio esp_driver_spi esp_timer lvgl esp_lcd_st7735
)
