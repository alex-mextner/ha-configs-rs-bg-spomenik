#pragma once

#include <algorithm>
#include <cstdint>

namespace presence_fade {

// Linear decay extracted from the existing corridor timer.
// A fade may reduce brightness, never increase it to reach its floor.
// Brightness arguments are fractions in [0, 1].
inline float level(float start, float configured_floor, uint32_t step,
                   uint32_t steps) {
  const float floor = std::min(start, configured_floor);
  if (steps == 0 || step >= steps)
    return floor;
  return start - (start - floor) * static_cast<float>(step) /
                     static_cast<float>(steps);
}

}  // namespace presence_fade
