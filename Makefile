VENV := .venv/hga
PYTHON_BIN ?= $(shell command -v python3.14 2>/dev/null || printf '%s' "$(HOME)/.pyenv/shims/python3.14")
UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
UV := UV_CACHE_DIR=$(UV_CACHE_DIR) uv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
RUFF := $(VENV)/bin/ruff
PYTEST := $(VENV)/bin/pytest
PYRIGHT := $(VENV)/bin/pyright
ENTITY_NOTE_VENV := .venv/entity-note
ENTITY_NOTE_PYTEST := $(ENTITY_NOTE_VENV)/bin/pytest
HGA_ROOT := submodules/home-generative-agent
HGA_TESTS := $(HGA_ROOT)/tests/custom_components/home_generative_agent
LOCAL_HGA_TESTS := tests/custom_components/home_generative_agent
PYTEST_ARGS := -q --import-mode=importlib
TEST_LOG_DIR := .cache/test-results

.PHONY: venv devdeps testdeps runtimedeps check lint format fix test test-standalone test-entity-note test-home-generative-agent test-home-generative-agent-deps test-copied-home-generative-agent test-regressions all clean typecheck

# Create venv and basic packaging tooling only. No linting here.
venv:
	$(UV) venv --allow-existing --python $(PYTHON_BIN) $(VENV)
	$(UV) pip install --python $(PY) -U pip setuptools wheel

devdeps: venv
	$(UV) pip install --python $(PY) -r requirements/dev.txt

testdeps: venv
	$(UV) pip install --python $(PY) -r requirements/test.txt

# Generate pinned runtime requirements from manifest and install them into the venv
runtimedeps: venv
	$(PY) scripts/gen_manifest_requirements.py
	$(UV) pip install --python $(PY) -r requirements_runtime_manifest.txt

check: venv
	$(PIP) check

# Non-mutating checks: enforce generated runtime requirements + lint + format
lint: devdeps
	$(PY) scripts/gen_manifest_requirements.py
	git diff --exit-code -- requirements_runtime_manifest.txt
	$(RUFF) format --check custom_components tests
	$(RUFF) check custom_components tests

# Mutating formatting
format: devdeps
	$(RUFF) format custom_components tests

# Mutating lint fixes (kept separate from format)
fix: devdeps
	$(RUFF) check --fix custom_components tests

# The full local suite is split by the Home Assistant version it requires.
test: test-standalone test-entity-note test-home-generative-agent test-copied-home-generative-agent

# Unlike the strict diagnostic above, allow only identical failures in unchanged
# copied HGA snapshots, freshly verified against the actual Git base.
test-regressions: test-standalone test-entity-note test-home-generative-agent
	$(PY) scripts/ha_hga_regression_check.py

test-standalone: test-entity-note-deps
	PYTHONPATH=$(CURDIR):$(CURDIR)/scripts $(ENTITY_NOTE_PYTEST) $(PYTEST_ARGS) $(wildcard tests/test_*.py)

# The U1 inspector/ghost tests live in 3d-cli (integrations/homeassistant/tests) and run in its
# gate; this repo's HA pytest plugin blocks the sockets and threads they use (alex-mextner/3d-cli#156).

$(ENTITY_NOTE_VENV)/bin/python:
	$(UV) venv --python $(PYTHON_BIN) $(ENTITY_NOTE_VENV)

test-entity-note-deps: $(ENTITY_NOTE_VENV)/bin/python
	$(UV) pip install --python $(ENTITY_NOTE_VENV)/bin/python -r requirements/test-entity-note.txt

test-entity-note: test-entity-note-deps
	PYTHONPATH=$(CURDIR) $(ENTITY_NOTE_PYTEST) $(PYTEST_ARGS) tests/custom_components/entity_note

test-home-generative-agent-deps: venv
	test -f $(HGA_ROOT)/requirements/test.txt
	$(UV) pip install --python $(PY) -r $(HGA_ROOT)/requirements/test.txt -r $(HGA_ROOT)/requirements_runtime_manifest.txt

test-home-generative-agent: test-home-generative-agent-deps
	mkdir -p $(TEST_LOG_DIR)
	cd $(HGA_ROOT) && PYTHONPATH=$(CURDIR)/$(HGA_ROOT) $(CURDIR)/$(PYTEST) $(PYTEST_ARGS) tests/custom_components/home_generative_agent > $(CURDIR)/$(TEST_LOG_DIR)/hga-source.log 2>&1; status=$$?; tail -n 40 $(CURDIR)/$(TEST_LOG_DIR)/hga-source.log; exit $$status

# Keep the strict copied-test diagnostic; do not hide existing failures.
test-copied-home-generative-agent: test-home-generative-agent-deps
	mkdir -p $(TEST_LOG_DIR)
	cd $(HGA_ROOT) && PYTHONPATH=$(CURDIR)/$(HGA_ROOT) $(CURDIR)/$(PYTEST) $(PYTEST_ARGS) $(CURDIR)/$(LOCAL_HGA_TESTS) > $(CURDIR)/$(TEST_LOG_DIR)/hga-copied.log 2>&1; status=$$?; tail -n 40 $(CURDIR)/$(TEST_LOG_DIR)/hga-copied.log; exit $$status

# Static type checking
typecheck: devdeps runtimedeps
	$(PYRIGHT) --stats

# Convenience target
all: devdeps testdeps runtimedeps lint test check typecheck

clean:
	rm -rf $(VENV)
