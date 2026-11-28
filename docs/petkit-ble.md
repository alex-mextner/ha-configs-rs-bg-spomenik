# PETKIT fountain over Bluetooth

PETKIT Eversweet Max 2 (`Petkit_CTW3_100`) uses the `petkit_ble` integration for local Bluetooth communication. Home Assistant can connect through a compatible Bluetooth adapter or an ESPHome Bluetooth proxy. No PETKIT cloud relay is needed for normal polling and control.

## Install and add the fountain

The integration source is the pinned `submodules/ha-petkit` Git submodule. `custom_components/petkit_ble` is a relative symlink into that source. Initialize the recorded submodule commit before restarting Home Assistant, and use the normal validated configuration reload procedure.

Open **Settings → Devices & services → Add integration → Petkit BLE**, then choose the discovered fountain.

- For an existing PETKIT app binding, choose **Use the device's existing secret (keeps the PetKit app working)** and supply the fountain's current secret. The setup verifies it without replacing the binding. Obtain the secret through an authenticated account diagnostic; keep it out of Git, chat transcripts and command history.
- A valid, complete identity reply indicating an unbound fountain permits normal initial setup.
- **Re-pair** replaces the existing binding. Use it only after deliberately choosing to replace app access.
- A connection error is not evidence that the fountain is unbound. Improve Bluetooth reception or retry the read.

The app and Home Assistant can retain the same secret, but simultaneous BLE connections depend on the fountain firmware. Close any active app connection when troubleshooting connection failures.

## Behavior and maintenance

The maintained source preserves upstream version 1.11.0 CTW3 fixes and adds existing-secret adoption plus a complete mandatory-state check. A missing or short CTW3 state response makes the poll fail instead of publishing zero values as valid readings.

`integration-sources.json` protects the maintained source branch. Keep the local safeguards when updating from upstream; do not replace this pin with the original upstream release merely because its version looks similar. The configured repository and the parent Git submodule commit identify the deployed source.

Bluetooth diagnostics should keep raw protocol debugging disabled: authentication frames contain the device secret. Read current device state before changing settings, and verify readback afterward. Do not use filter resets, history deletion or pump shutdown as connectivity tests.

## Sources

- [Maintained integration](https://github.com/alex-mextner/ha-petkit)
- [Upstream integration](https://github.com/aavdberg/ha-petkit)
- [Upstream existing-secret adoption change](https://github.com/aavdberg/ha-petkit/pull/159)
