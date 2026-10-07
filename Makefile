# Continuous Claude v4.7 - developer tasks.
# Recipes are POSIX sh: run from Git Bash on Windows (make needs sh.exe on PATH) or any
# POSIX shell. Without make, use the PowerShell twin: pwsh install/setup.ps1 -Setup|-Test|...
# Override the interpreter per call, e.g. `make test PYTHON=python3` on Linux/macOS.

PYTHON ?= py -3.13
RUFF ?= $(PYTHON) -m ruff

.DEFAULT_GOAL := help
.PHONY: help setup test lint format typecheck readiness sync

help: ## List targets
	@echo "Usage: make <target> [PYTHON='py -3.13']"
	@echo ""
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-10s %s\n", $$1, $$2}'

setup: ## Deps (requirements.lock, else pyproject) + plotly-resampler --no-deps, Playwright Chromium, pre-commit hook; idempotent
	$(PYTHON) install/setup_deps.py
	@if $(PYTHON) -m playwright --version >/dev/null 2>&1; then \
	  echo "$(PYTHON) -m playwright install chromium"; \
	  $(PYTHON) -m playwright install chromium; \
	else \
	  echo "skip: playwright not installed"; \
	fi
	@if [ ! -f .pre-commit-config.yaml ]; then \
	  echo "skip: no .pre-commit-config.yaml"; \
	elif $(PYTHON) -m pre_commit --version >/dev/null 2>&1; then \
	  echo "$(PYTHON) -m pre_commit install"; \
	  $(PYTHON) -m pre_commit install; \
	else \
	  echo "skip: pre-commit not installed"; \
	fi

test: ## Unit suites (pytest) + hook suites + readiness script tests
	@failed=""; \
	echo "== pytest"; \
	$(PYTHON) -m pytest -q || failed="$$failed pytest"; \
	for t in .claude/hooks/test_*.sh; do \
	  echo "== $$t"; \
	  bash "$$t" || failed="$$failed $$t"; \
	done; \
	if [ -f scripts/test_readiness.sh ]; then \
	  echo "== scripts/test_readiness.sh"; \
	  bash scripts/test_readiness.sh || failed="$$failed scripts/test_readiness.sh"; \
	fi; \
	if [ -n "$$failed" ]; then echo "FAILED:$$failed"; exit 1; fi; \
	echo "ALL SUITES PASSED"

lint: ## ruff check
	$(RUFF) check .

format: ## ruff format (rewrites files)
	$(RUFF) format .

typecheck: ## mypy (config in pyproject.toml)
	$(PYTHON) -m mypy

readiness: ## Agent-readiness report (scripts/readiness.sh, ~30-50s, foreground)
	bash scripts/readiness.sh

sync: ## Copy harness into ~/.claude (WRITES ~/.claude; dry run: install/sync_global.py --diff)
	@echo "WARNING: sync writes into ~/.claude (backups go to ~/.claude/.ccv47-backup/)." >&2
	@echo "         Dry run first: $(PYTHON) install/sync_global.py --diff" >&2
	$(PYTHON) install/sync_global.py --apply
