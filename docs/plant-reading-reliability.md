# Plant reading reliability

The companion reports an invalid-reading error whenever the displayed measurement
remains numerically unchanged for more than twelve hours. This applies
to 0%, 100%, and intermediate values. A clipped value can reflect a real physical
condition; the error means it no longer provides a trustworthy numerical moisture
estimate. Physical sensor diagnosis is a separate step. The twelve-hour threshold
is fixed; configuration cannot shorten or extend it.

The companion persists the last real numeric change independently of physical
sample timestamps. Identical fresh reports, HA restarts, and unavailable/restored
states do not restart the twelve-hour clock. Recorder bootstrap includes the state
at the beginning of its bounded fourteen-day history. Unavailable history fails
closed and retries with bounded backoff; previously persisted change times remain
usable. Independent fresh numeric changes can also establish recovery when
Recorder is unavailable; a repeated initial value never starts a new healthy
twelve-hour grace period.

A fault hides the numeric moisture estimate and disables new watering inference,
forecasts, and current-condition voice reminders. Previously confirmed dry warnings
remain in the record and are explicitly labelled as historical on the card. Check
the substrate manually while the reading is invalid. Lower clipping is identified
separately and does not claim the soil is wet or the hardware is broken.

A transient recovery cannot clear the fault. Recovery needs three independent
physical reports spanning ten minutes; frozen-value incidents additionally require
varied values away from the frozen/clipped baseline. When a separate raw source is
configured, its measurements must vary too. Changing or removing that mapping
resets only the raw-source clock; persisted invalid intervals and confirmation
requirements remain in force.
If a startup transport error later reveals a frozen-value incident, its identity
is preserved and the recovery requirement becomes stricter.

New watering inference accepts trustworthy dry baselines at or below the dry
calibration. Evidence from an invalid interval, including a long 0% plateau,
cannot establish an automatic watering event. Manual and upstream reports remain
valid.
It rejects a baseline already at or above the wet calibration. Earlier automatic
inference treated fluctuations above the wet calibration as watering evidence.
A cached automatic estimate is
retracted only when retained evidence proves it unsupported or it belongs to the
current invalid interval. Rejected automatic event timestamps remain in a bounded
retention list so rescanning retained samples cannot recreate them after recovery.
Manual watering records are preserved. Persisted invalid intervals also quarantine
all inference evidence from the untrusted plateau start or invalid numeric
measurement through demonstrated recovery, including
recovery samples that could otherwise create a new automatic event after the fault
clears. Intervals are retained for the same fourteen-day inference window.
An absent physical report or missing displayed index contains no new moisture
evidence to quarantine. A brief missing poll therefore preserves previously
trustworthy trend and wet history
once recovery is confirmed. Real gaps over the freshness limit still break
continuity through the existing sample-gap checks.

Forecasts and prolonged-wet estimates use the contiguous trustworthy history
after the most recent invalid interval. Normal dryness confirmation resumes when
the fresh sample window is sufficient. When Recorder becomes available, a recently
stable value can recover through independent fresh samples.

Until HA reaches `CoreState.running`, missing states remain untrusted without
opening a new latched incident, including during its starting phase. Known old
plateaus and existing incidents still fault immediately.
Persistent error notices describe the reliability problem generally; the card and
Alice show the current reason.

## Published state

`stagnant_seconds` records the duration at the last state publication.
Consumers use `last_value_change_at` to calculate the current duration without
requiring the integration to write its full stored state every minute.

The deterministic recovery controller is documented in
[plant-sensor-recovery.md](plant-sensor-recovery.md). It operates on the persisted
incident identity, attempts bounded physical remeasurement and supported recovery
first, and starts one tracked OMP session only when these measures do not resolve
the incident. It never invents calibration points or starts automatic irrigation.
