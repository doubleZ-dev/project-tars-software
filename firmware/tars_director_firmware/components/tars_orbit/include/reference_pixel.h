#pragma once
#include "orbit_model.h"

/* Reference-specific palette and 9-column solid-cell silhouettes, no brows/pupils.
 * Animation is generated from native pixel cells. Boot follows the measured video. */
void pixel_frame(orbit_id_t id, unsigned eye, uint32_t elapsed, uint32_t duration,
                 unsigned voice_level, orbit_frame_t *out);
