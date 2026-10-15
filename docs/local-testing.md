# Local verification

`make test` is the strict full diagnostic. It runs every root-level script test,
local entity_note tests, the HGA submodule's maintained tests, and the historical
HGA tests copied into this repository. Failures remain failures; no xfail or
ignore list is added.

HGA tests use the dependency versions from the checked-out HGA submodule. The
entity_note suite uses the separately pinned deployed Home Assistant version.
Environments are isolated in `.venv/hga` and `.venv/entity-note`. The HGA tests
run from the source directory because some inspect source via relative paths.
The build host needs Python 3.14, uv, Docker Compose and fakeroot. Tests never
write the real host's system configuration; installer tests redirect paths and
fake only operating-system boundaries.

`make test-regressions`, selected by `.ship-config`, also requires all maintained
suites to pass. It additionally runs the copied HGA tests on the candidate. When
those pass, there is no baseline exception. If they fail, the exception is only
available when the HGA source gitlink, the actual checked-out source, copied
tests and pytest configuration are unchanged from the merge base with
`origin/main`, and the source tree is clean. It extracts the real base tests from
Git and runs both inventories in the same interpreter and source directory.
Every test ID and outcome must match exactly: a new failure, removed test,
changed skip, collection error, empty report or different exit code is fatal.
Full JUnit and console reports remain under `.cache/test-results`; the command
prints each existing failure explicitly. This is a no-regression verdict, not a
claim that existing failing tests passed. Changes to HGA require the strict
copied suite to pass rather than inheriting the exception.

Observed on 2026-10-16 against base aaef53e: the maintained HGA source suite passes
3425 tests with one upstream skip. The historical copied suite has exactly
1119 results: 1039 pass and 80 fail on both base and candidate. Bluetooth code,
configuration and tests are not part of this exception.
