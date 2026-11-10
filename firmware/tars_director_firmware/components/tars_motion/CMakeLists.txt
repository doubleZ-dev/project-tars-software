idf_component_register(SRCS "tars_motion.c" "tars_servo_mcpwm.c" "tars_emotion.c" INCLUDE_DIRS "include" REQUIRES tars_board tars_core esp_driver_mcpwm)
