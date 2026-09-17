.PHONY: help verify verify-quick test install demo clean lint

help:
	@echo "make install       install the agent and its dependencies"
	@echo "make verify        every acceptance check for every phase (zero credentials)"
	@echo "make verify-quick  the same, skipping the slow whole-year checks"
	@echo "make test          the pytest suite"
	@echo "make demo          a real backtest plus a parity proof, no keys needed"

install:
	python -m pip install -e .

verify:
	python -m ee_agent.verify

verify-quick:
	python -m ee_agent.verify --quick

test:
	python -m pytest tests/ -q

demo:
	python -m ee_agent.cli.main demo

clean:
	rm -rf .pytest_cache **/__pycache__ build dist *.egg-info
