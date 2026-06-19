.PHONY: verify test invariants golden smoke

verify:        ## full verification (the session start/end gate)
	./scripts/verify.sh

test:          ## fast suite, excluding slow backtests
	PYTHONPATH=src python -m pytest -q -m "not slow"

invariants:    ## the CLAUDE.md §5 immunity-spec guards
	PYTHONPATH=src python -m pytest tests/invariants -q

golden:        ## known-answer reconciliation vs tests/golden/
	PYTHONPATH=src python -m pytest tests/golden -q

smoke:         ## M0 data-access smoke test (must pass before other M0 code)
	PYTHONPATH=src python scripts/smoke_data_access.py
