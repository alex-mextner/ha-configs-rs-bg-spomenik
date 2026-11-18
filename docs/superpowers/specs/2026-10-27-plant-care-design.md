# Plant care alerts

Approved scope: reusable plant cards on the existing Home dashboard, orange watering-due and red >24h-overdue states, phone notifications, hourly living-room Yandex Station reminders once overdue, own-photo upload and multiple plants. No sidebar panel. Preserve current dry/wet ADC calibration and unrelated production work.

Approaches: a new plant integration duplicates photo/setup/history; a large fork makes updates costly; use MIT Adaptive Plant unchanged as a pinned submodule, plus a small MIT companion and delivery configuration in this HA repository (selected). The companion only adds robust alert timing; upstream owns plant identity, photo upload and watering buttons.

Plant settings and source sensors come from Adaptive Plant. For the noisy resistive sensor, a rolling 30-minute trimmed mean requires >=4 independent samples before starting a dry timer. Default dry threshold 25%, recovery 35%, confirmation 6h; overdue starts 24h after the orange warning. Stale/invalid data suppress watering reminders and show a separate sensor problem. State survives restart via HA Store. Manual watering clears reminders immediately; snooze pauses 24h. New plants are discovered automatically.

Instance delivery: use verified mobile notify service and media_player.yandex_station_m00zrw3008rajg. Voice only 08:00–22:00 local, once per hour, with names aggregated. No automatic irrigation. TTS test must not ignore quiet hours. Tests simulate time/state without falsifying live measurements or watering history. Screenshots of simulated warnings must be labelled.

Photo: no user's plant photo is supplied in this conversation; show a neutral placeholder and provide native upstream upload UI, not an invented photograph.
