idf_component_register(SRCS "tars_protocol.c" INCLUDE_DIRS "include"
                       REQUIRES tars_orbit tars_usb tars_board tars_audio tars_core tars_motion tars_display tars_wifi_audio tars_xiaozhi_audio json esp_timer)
