"""Shared calibration limits and canonical evidence identity."""

import hashlib
import json

ADC_MAX = 4095
INDEX_MAX = 100
MIN_BURST_READS = 5
MAX_BURST_READS = 64
MIN_TRAINING_CYCLES = 3
MIN_TOTAL_CYCLES = 4
DAY = 86400.0
NOMINAL_SAMPLE_SECONDS = 300.0
# Ten percent scheduling jitter is allowed between independent physical votes.
MIN_VOTE_SPACING = 270.0
MAX_TOTAL_DRIFT_FRACTION = 0.05
MAX_STEP_FRACTION = 0.01
WINDOW_SPREAD_FRACTION = 0.05
BURST_SPREAD_FRACTION = 0.1
MIN_MAD_COUNTS = 2.0
MAD_SPAN_FRACTION = 0.02
MIN_SPREAD_COUNTS = 6.0
MIN_TREND_COUNTS = 2.0
TREND_SPAN_FRACTION = 0.02
RAW_MEDIAN_TOLERANCE = 1.0
MIN_DEADBAND_COUNTS = 2.0
DEADBAND_SPAN_FRACTION = 0.005
MIN_WINDOW_SECONDS = 3600.0
MAX_WINDOW_SECONDS = 7200.0
MIN_VOTES = 12
MAX_GAP_SECONDS = 600.0
TRAINING_SPAN_SECONDS = 14 * DAY
COOLDOWN_SECONDS = 7 * DAY
BARRIER_SECONDS = 1800
HISTORY_SECONDS = 90 * DAY
HOLDOUT_MAX_AGE = DAY
CURRENT_MAX_AGE = 1200.0
MAX_SAMPLES = 30000
MAX_REFERENCES = 256
EPSILON = 1e-9


def within_envelope(original_dry: float, original_wet: float, target: float) -> bool:
    """Check a target against an already validated immutable original baseline."""
    span = original_wet - original_dry
    return (
        original_dry < target <= ADC_MAX
        and abs(target - original_wet) <= MAX_TOTAL_DRIFT_FRACTION * span + EPSILON
    )


def canonical_digest(value: object) -> str:
    """Keep event and evidence identities stable across all calibration layers."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
