#include "../libs/fade_profile.hpp"

#include <cmath>
#include <cstdio>
#include <cstring>

struct Case {
  const char *name;
  float start;
  float floor;
  unsigned step;
  unsigned steps;
  float expected;
};

int main(int argc, char **argv) {
  // Hand-calculated outcomes; raising a night level to the floor is a bug.
  const Case cases[] = {
      {"day starts at actual brightness", 0.80f, 0.10f, 0, 180, 0.80f},
      {"day halfway through thirty minutes", 0.80f, 0.10f, 90, 180, 0.45f},
      {"day reaches the floor", 0.80f, 0.10f, 180, 180, 0.10f},
      {"late step does not undershoot", 0.80f, 0.10f, 181, 180, 0.10f},
      {"night level never rises", 0.05f, 0.10f, 90, 180, 0.05f},
      {"night floor stays below configured floor", 0.05f, 0.10f, 180, 180, 0.05f},
      {"already off stays off", 0.00f, 0.10f, 90, 180, 0.00f},
      {"restart begins at current level", 0.35f, 0.10f, 0, 180, 0.35f},
      {"restart has its own full duration", 0.35f, 0.10f, 90, 180, 0.225f},
      {"zero steps finishes without division", 0.80f, 0.10f, 0, 0, 0.10f},
  };
  unsigned failures = 0;
  unsigned executed = 0;
  for (const auto &c : cases) {
    if (argc > 1 && std::strcmp(argv[1], c.name) != 0)
      continue;
    ++executed;
    const float actual =
        presence_fade::level(c.start, c.floor, c.step, c.steps);
    if (!std::isfinite(actual) || std::fabs(actual - c.expected) > 0.00001f) {
      std::fprintf(stderr, "%s: expected %.5f, got %.5f\n", c.name,
                   c.expected, actual);
      ++failures;
    }
  }
  std::printf("Fade profile: %u scenarios, %u failures\n", executed, failures);
  if (executed == 0)
    return 2;
  return failures ? 1 : 0;
}
