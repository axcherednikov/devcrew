.DEFAULT_GOAL := help

PYTHON ?= python3
JOBS ?= 4
TESTS ?=

.PHONY: help test test-serial check-package

help:
	@printf '%s\n' \
	  'make test [JOBS=4] [TESTS="module.Class module.Class.test_method"]  Run the full suite or selected tests.' \
	  'make test-serial [TESTS="module.Class"]                      Run selected tests in one unittest process.' \
	  'make check-package                                          Validate package metadata and documentation links.'

test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -B scripts/run_tests.py --jobs $(JOBS) $(TESTS)

test-serial:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -B scripts/run_tests.py --jobs 1 $(TESTS)

check-package:
	$(PYTHON) -B scripts/check_package.py
