idf_component_register(
    SRCS "tars_audio.c"
    INCLUDE_DIRS "include"
    REQUIRES tars_orbit tars_board esp_driver_gpio esp_driver_i2s freertos
)
