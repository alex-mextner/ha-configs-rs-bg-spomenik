# Verification

Run tests and builds only in genuine native CI or the established managed RunPod
runner. Workstation work is limited to source inspection, edits, and bounded
static checks. Setting `CI=1` on a household machine does not make it a CI runner.

## Exact source receipts for shipping

`.ship-config` selects `python3 scripts/ci/wait_ha_regressions.py`. This command
reads Git and GitHub metadata; it does not execute application tests or builds.
Push the reviewed commit first, then run the receipt check from a clean worktree
and index. Untracked files and changed submodules also make the check fail.

The required evidence is a successful **push** run for the exact local `HEAD` in
each workflow:

| Workflow | Required successful jobs |
| --- | --- |
| `ha-regressions.yml` | `maintained-suites` |
| `kodi-fireplace.yml` | `host-controller`, `home-assistant-script` |
| `lint.yml` | `Ruff` |

A pull-request run tests GitHub's merge tree and cannot replace push evidence,
even when its metadata reports the same head SHA. Manual runs also do not satisfy
this gate. The newest push run for that commit must pass; an older successful
run cannot hide a newer failure. Skipped, absent, or duplicate required jobs are
not passes. The helper rechecks the clean worktree and `HEAD` after waiting.

The helper prints progress and keeps a private automatic log under `/tmp`. Safe
command labels, return codes, and diagnostic categories help distinguish a CI
failure from an authentication or metadata transport failure without retaining
credentials or raw URL-bearing error messages.

## Native regression commands

`make test` is the strict full diagnostic to run on the approved remote runner.
It runs every root-level script test, local entity_note tests, the HGA submodule's
maintained tests, and the historical HGA tests copied into this repository.
Failures remain failures; no xfail or ignore list is added.

HGA tests use the dependency versions from the checked-out HGA submodule. The
entity_note suite uses the separately pinned deployed Home Assistant version.
Environments are isolated in `.venv/hga` and `.venv/entity-note`. The HGA tests
run from the source directory because some inspect source via relative paths.
The runner needs Python 3.14, uv, Docker Compose, and fakeroot. Installer tests
redirect system paths and fake only operating-system boundaries.

`make test-regressions`, executed by the maintained native CI job, requires all
maintained suites to pass and also runs the copied HGA tests on the candidate.
When those pass, there is no baseline exception. If they fail, the exception is
available only when the HGA source gitlink, actual checked-out source, copied
tests, and pytest configuration are unchanged from the merge base with
`origin/main`, and the source tree is clean. It extracts the real base tests from
Git and runs both inventories in the same interpreter and source directory.
Every test ID and outcome must match exactly: a new failure, removed test,
changed skip, collection error, empty report, or different exit code is fatal.
Full JUnit and console reports remain under `.cache/test-results`; the command
prints each existing failure explicitly. This is a no-regression verdict, not a
claim that existing failing tests passed. Changes to HGA require the strict
copied suite to pass rather than inheriting the exception.

Historical observation on 2026-10-16 against base `aaef53e`: the maintained HGA
source suite passed 3,425 tests with one upstream skip. The historical copied
suite had exactly 1,119 results: 1,039 passed and 80 failed on both base and
candidate. Bluetooth code, configuration, and tests are not part of this
exception. These historical counts do not replace a fresh candidate receipt.
