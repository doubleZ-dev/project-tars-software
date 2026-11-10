#include "tars_motion.h"

#include "driver/mcpwm_prelude.h"
#include "esp_check.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define SERVO_MIN_PULSE_US 500
#define SERVO_MAX_PULSE_US 2500
#define SERVO_PERIOD_US 20000

static mcpwm_cmpr_handle_t s_comparators[TARS_SERVO_COUNT];
static mcpwm_gen_handle_t s_generators[TARS_SERVO_COUNT];

static uint32_t physical_to_pulse(int16_t angle) {
    if (angle < -90) angle = -90;
    if (angle > 90) angle = 90;
    return SERVO_MIN_PULSE_US + (uint32_t)(angle + 90) * (SERVO_MAX_PULSE_US - SERVO_MIN_PULSE_US) / 180;
}

void tars_servo_init(void) {
    mcpwm_timer_handle_t timers[2];
    mcpwm_oper_handle_t operators[2];
    mcpwm_timer_config_t timer_config = {
        .group_id = 0, .clk_src = MCPWM_TIMER_CLK_SRC_DEFAULT,
        .resolution_hz = 1000000, .period_ticks = SERVO_PERIOD_US,
        .count_mode = MCPWM_TIMER_COUNT_MODE_UP,
    };
    mcpwm_operator_config_t operator_config = {.group_id = 0};
    for (int i = 0; i < 2; ++i) {
        ESP_ERROR_CHECK(mcpwm_new_timer(&timer_config, &timers[i]));
        ESP_ERROR_CHECK(mcpwm_new_operator(&operator_config, &operators[i]));
        ESP_ERROR_CHECK(mcpwm_operator_connect_timer(operators[i], timers[i]));
    }
    for (int i = 0; i < TARS_SERVO_COUNT; ++i) {
        mcpwm_comparator_config_t cc = {.flags.update_cmp_on_tez = true};
        mcpwm_generator_config_t gc = {.gen_gpio_num = TARS_SERVO_CALIBRATION[i].gpio};
        ESP_ERROR_CHECK(mcpwm_new_comparator(operators[i / 2], &cc, &s_comparators[i]));
        ESP_ERROR_CHECK(mcpwm_new_generator(operators[i / 2], &gc, &s_generators[i]));
        ESP_ERROR_CHECK(mcpwm_generator_set_action_on_timer_event(s_generators[i],
            MCPWM_GEN_TIMER_EVENT_ACTION(MCPWM_TIMER_DIRECTION_UP, MCPWM_TIMER_EVENT_EMPTY, MCPWM_GEN_ACTION_HIGH)));
        ESP_ERROR_CHECK(mcpwm_generator_set_action_on_compare_event(s_generators[i],
            MCPWM_GEN_COMPARE_EVENT_ACTION(MCPWM_TIMER_DIRECTION_UP, s_comparators[i], MCPWM_GEN_ACTION_LOW)));
        ESP_ERROR_CHECK(mcpwm_generator_set_force_level(s_generators[i], 0, true));
    }
    for (int i = 0; i < 2; ++i) {
        ESP_ERROR_CHECK(mcpwm_timer_enable(timers[i]));
        ESP_ERROR_CHECK(mcpwm_timer_start_stop(timers[i], MCPWM_TIMER_START_NO_STOP));
    }
}

void tars_servo_output_enable(void) {
    for (int i = 0; i < TARS_SERVO_COUNT; ++i) {
        ESP_ERROR_CHECK(mcpwm_generator_set_force_level(s_generators[i], -1, true));
    }
}

void tars_servo_output_disable(void) {
    for (int i = 0; i < TARS_SERVO_COUNT; ++i) {
        ESP_ERROR_CHECK(mcpwm_generator_set_force_level(s_generators[i], 0, true));
    }
}

void tars_servo_write_physical(const int16_t angles[TARS_SERVO_COUNT]) {
    for (int i = 0; i < TARS_SERVO_COUNT; ++i) {
        ESP_ERROR_CHECK(mcpwm_comparator_set_compare_value(s_comparators[i], physical_to_pulse(angles[i])));
    }
}

void tars_servo_write_physical_float(const float angles[TARS_SERVO_COUNT]) {
    /* Emotion trajectories retain sub-degree precision until 1us PWM rounding. */
    for(int i=0;i<TARS_SERVO_COUNT;++i) {
        float value=angles[i];
        if(value < -90) value=-90;
        if(value > 90) value=90;
        uint32_t pulse=(uint32_t)(SERVO_MIN_PULSE_US+(value+90.0f)*
            (SERVO_MAX_PULSE_US-SERVO_MIN_PULSE_US)/180.0f+0.5f);
        ESP_ERROR_CHECK(mcpwm_comparator_set_compare_value(s_comparators[i],pulse));
    }
}

void tars_servo_calibration_write(int servo, int angle_deg) {
    /* Caller holds motion gate; all generators stay forced LOW while the
       comparator update waits for a full 50Hz period. No stale first pulse. */
    tars_servo_output_disable();
    ESP_ERROR_CHECK(mcpwm_comparator_set_compare_value(s_comparators[servo - 1], physical_to_pulse(angle_deg)));
    vTaskDelay(pdMS_TO_TICKS(20) + 1);
    ESP_ERROR_CHECK(mcpwm_generator_set_force_level(s_generators[servo - 1], -1, true));
}
