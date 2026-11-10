idf_component_register(SRCS "tars_xiaozhi_audio.c" INCLUDE_DIRS "include"
 REQUIRES tars_wifi_audio tars_audio tars_core tars_motion tars_display esp_websocket_client esp_audio_codec json esp_timer esp_wifi esp_hw_support
 EMBED_TXTFILES "companion.pem")
