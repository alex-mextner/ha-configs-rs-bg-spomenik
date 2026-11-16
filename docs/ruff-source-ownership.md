# Ruff source ownership

The Home Assistant repository applies its Python 3.13 `ALL` rule selection and formatter to first-party Python files. The lint workflow continues to run `ruff check .` and `ruff format . --check` across that scope.

## Pinned 3D integration

The `3d-cli` submodule is maintained in [alex-mextner/3d-cli](https://github.com/alex-mextner/3d-cli). The repository's `AGENTS.md` assigns 3D source changes to that repository and requires `3d ha deploy` when deploying a new upstream pin. Five local symlinks expose files from the same pinned tree:

| HA path | Target inside `3d-cli` |
| --- | --- |
| `scripts/u1_gcode_index.py` | `integrations/homeassistant/scripts/u1_gcode_index.py` |
| `scripts/u1_ghost.py` | `integrations/homeassistant/scripts/u1_ghost.py` |
| `scripts/u1_mjpeg_proxy.py` | `integrations/homeassistant/scripts/u1_mjpeg_proxy.py` |
| `scripts/u1_print_inspector.py` | `integrations/homeassistant/scripts/u1_print_inspector.py` |
| `scripts/u1_share.py` | `integrations/homeassistant/scripts/u1_share.py` |

The parent Ruff configuration excludes the root-relative `./3d-cli` directory and those five exact paths. It preserves Ruff's default exclusions and all first-party rules. There is no wildcard exclusion for other `u1_*` files. Neither the submodule pin nor the symlink bytes are changed by this boundary.

Upstream owns a Python 3.10 policy with `E4`, `E7`, `E9`, `F`, `PERF`, `C4`, `SIM`, `B`, and `UP` rules. Applying the parent's `ALL` policy through the symlinks duplicates the same source under a different policy. The source boundary keeps maintenance and validation with the source owner.

## Known upstream gaps at the pinned revision

The revision inspected on 2026-11-15 was [`4ce0e3241bf02c77e1eab3483bdf2597b9d9afb6`](https://github.com/alex-mextner/3d-cli/tree/4ce0e3241bf02c77e1eab3483bdf2597b9d9afb6). At that revision, `tests/run_gate.py` invokes Ruff on `lib/` and `tests/`. The Home Assistant integration has pytest and strict YAML checks, but its Python paths and `tools/` are outside those Ruff operands.

The parent CI exposure found 36 diagnostics under upstream's own policy: 28 in Home Assistant integration paths and eight in `tools/`. Three integration diagnostics are `B023` loop-capture findings. Separately, the five symlinks produced 546 findings under the parent's different `ALL` policy. Excluding these owned paths does not fix either set of upstream diagnostics or establish a green upstream revision.

The exact pinned push also has recorded failing gates:

- [CI run 38988320557](https://github.com/alex-mextner/3d-cli/actions/runs/38988320557): two failed tests, 5,925 passed and 32 skipped; coverage was 79.78% against the existing 80% requirement.
- [CodeQL run 38988320575](https://github.com/alex-mextner/3d-cli/actions/runs/38988320575): two gated JavaScript findings in `lib/web/static/app.js`.

The earlier parent workflow did not initialize this submodule. Its missing symlink targets do not constitute evidence that the source previously passed the parent gate. Initializing the exact pin made the separate debt visible; no historical green check is claimed.

## Maintaining the boundary

Repair upstream findings in `3d-cli` through its normal source review and CI. Any expansion of its Ruff operands must retain the relevant runtime tests and coverage gate. A new runtime pin must follow the existing `3d ha deploy` process. Reassess this boundary if source ownership changes or if a symlink becomes an independently maintained first-party file.

Keep first-party HA changes subject to the full parent lint and regression checks. Record upstream gate failures as upstream work; do not suppress them in the upstream repository or present a passing parent check as proof that the pinned upstream checks passed.
