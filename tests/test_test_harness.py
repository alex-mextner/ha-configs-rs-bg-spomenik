# Copyright 2026 Project contributors
# ruff: noqa: INP001, S101
"""
Regression checks for the repository-local test runner.

Accessed via: ``make test`` runs this file in the standalone suite before the
Home Assistant integration suites. Assumptions: the Makefile remains the
single local entrypoint and gh ship reads ``.ship-config``. Past bugs: root
pytest discovery collected copied and submodule HGA tests together, against
an incompatible dependency set.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_local_runner_uses_explicit_isolated_suites() -> None:
    makefile = (ROOT / "Makefile").read_text()

    assert (
        "test: test-standalone test-entity-note test-home-generative-agent" in makefile
    )
    assert "test-copied-home-generative-agent:" in makefile
    assert ".venv/entity-note" in makefile
    assert ".venv/hga" in makefile
    assert "UV_CACHE_DIR ?= $(CURDIR)/.cache/uv" in makefile
    assert "venv --allow-existing" in makefile
    assert "PYTEST_ARGS := -q --import-mode=importlib" in makefile
    assert "TEST_LOG_DIR := .cache/test-results" in makefile
    assert "tests/custom_components/entity_note" in makefile
    assert "tests/custom_components/home_generative_agent" in makefile
    assert "HGA_ROOT := submodules/home-generative-agent" in makefile
    assert (
        "HGA_TESTS := $(HGA_ROOT)/tests/custom_components/home_generative_agent"
        in makefile
    )
    assert "PYTHONPATH=$(CURDIR)/$(HGA_ROOT)" in makefile
    assert "cd $(HGA_ROOT) && PYTHONPATH=$(CURDIR)/$(HGA_ROOT)" in makefile
    assert (
        "$(CURDIR)/$(PYTEST) $(PYTEST_ARGS) tests/custom_components/home_generative_agent"
        in makefile
    )
    assert "tail -n 40 $(CURDIR)/$(TEST_LOG_DIR)/hga-source.log" in makefile


def test_ship_uses_the_local_test_entrypoint() -> None:
    ship_config = (ROOT / ".ship-config").read_text()

    assert "SHIP_LOCAL_TEST_CMD=make test-regressions" in ship_config
    makefile = (ROOT / "Makefile").read_text()
    assert (
        "test: test-standalone test-entity-note test-home-generative-agent test-copied-home-generative-agent"
        in makefile
    )
    assert "$(PY) scripts/ha_hga_regression_check.py" in makefile
    assert "$(wildcard tests/test_*.py)" in makefile
