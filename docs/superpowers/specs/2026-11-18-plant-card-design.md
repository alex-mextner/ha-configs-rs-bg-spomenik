# Compact plant card and automatic watering presentation

## Intent and scope

The dashboard shows one compact tile per discovered plant. The entire tile,
including its photo, opens the existing detailed view. A tile shows its name,
trusted relative moisture, short status and last watering. Reliability,
forecast, source history, calibration history and existing settings/photo upload
remain in the detail view. Approximate reports retain day-only precision.

## Approach

Keep the existing custom element. A persistent shadow-DOM shell, entity-keyed
tile buttons and a native modal dialog retain node identity during ordinary HA
updates. Unchanged fragments, photos and graphs remain untouched. This is the
builder's implementation choice for the user's requested tile → detail flow;
a new framework or dashboard is unnecessary.

The dialog closes by its close button or Escape and returns focus to its tile.
Photo upload is inside the dialog. Per-plant photos retain alpha and contain
sizing; the Strelitzia image is never used as a default for other plants.

## Graph and event meaning

Click, touch and arrow keys select the nearest real valid Recorder point.
A visible output gives its actual date/time and relative index; no interpolated
measurement is invented. Unknown gaps remain gaps. Plant/range/selected
timestamp, open dialog and disclosure state survive ordinary state updates.

A report of watering on approximately17November displays an approximation marker
and day, without an invented midnight. Automatic detections retain their
reported interval, detection time and retroactive provenance. The UI creates
neither watering events nor calibration references.

## Verification

Bounded Node tests check compact content, hidden invalid percentages, day-only
labels and real-point selection. Established CI Playwright checks actual DOM
identity, focus, dialog, pointer/keyboard inspection and disclosure persistence.
Separate synthetic interaction scenarios never become production screenshots.

The real19:47 pre-feature snapshot and served4a9059 card remain before evidence.
Final screenshots use a new actual after snapshot and exact deployed component
and served-photo bytes. Ordinary review/CI/deployment/task/ship gates remain.
