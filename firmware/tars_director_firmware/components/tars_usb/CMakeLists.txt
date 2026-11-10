idf_component_register(SRCS "tars_usb.c" INCLUDE_DIRS "include"
    REQUIRES esp_driver_usb_serial_jtag freertos log)
