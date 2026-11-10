#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef enum { TARS_DIAG_BOOT, TARS_DIAG_ARM, TARS_DIAG_GAIT_START,
    TARS_DIAG_TARGET, TARS_DIAG_HOLD, TARS_DIAG_COMPLETE, TARS_DIAG_CANCELLED } tars_diag_stage_t;
typedef struct {
    uint32_t uptime_ms, stage;
    int32_t action;
    uint32_t cycle, phase;
    char command_id[40];
} tars_diag_mark_t;

void tars_diag_init(void);
void tars_diag_log_boot(void);
int tars_diag_reset_reason(void);
const char *tars_diag_reset_name(void);
const char *tars_diag_stage_name(uint32_t stage);
void tars_diag_mark(tars_diag_stage_t stage, int action, unsigned cycle, unsigned phase, const char *id);
bool tars_diag_get(bool previous, tars_diag_mark_t *out);
