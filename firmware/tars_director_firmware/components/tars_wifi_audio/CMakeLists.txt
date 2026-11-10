idf_component_register(SRCS "tars_wifi_audio.c" INCLUDE_DIRS "include"
    REQUIRES tars_audio tars_core tars_display tars_motion esp_wifi esp_netif esp_event
             nvs_flash esp_timer esp_hw_support mbedtls lwip json)
