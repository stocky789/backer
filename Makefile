.PHONY: install install-dev setup test test-quick lint format type-check clean build run-server demo help

# Default target
help:
	@echo "Backer - Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make install      Install backer in production mode"
	@echo "  make install-dev  Install backer with dev dependencies"
	@echo "  make setup        Install backer and download backup tools"
	@echo ""
	@echo "Development:"
	@echo "  make test         Run all tests"
	@echo "  make test-quick   Run quick tests (no integration)"
	@echo "  make lint         Run linter"
	@echo "  make format       Format code"
	@echo "  make type-check   Run type checker"
	@echo ""
	@echo "Build:"
	@echo "  make build        Build distribution packages"
	@echo "  make clean        Remove build artifacts"
	@echo ""
	@echo "Run:"
	@echo "  make run-server   Start the backup server"
	@echo "  make demo         Run a demo backup"

# Installation
install:
	pip install -e ".[all]"

install-dev:
	pip install -e ".[dev,all]"

setup: install
	backer setup

# Testing
test:
	pytest tests/ -v --cov=src/backer --cov-report=term-missing

test-quick:
	pytest tests/ -v -m "not integration" --ignore=tests/integration/

# Code quality
lint:
	ruff check src/ tests/

format:
	ruff format src/ tests/
	ruff check --fix src/ tests/

type-check:
	mypy src/backer/

# Build
build: clean
	python -m build

clean:
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	rm -rf htmlcov/ .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Run
run-server:
	backer server start

# Demo - creates test data and runs a backup
demo:
	@echo "Creating test data..."
	@mkdir -p /tmp/backer-demo/source /tmp/backer-demo/dest
	@echo "Hello World" > /tmp/backer-demo/source/test.txt
	@echo "Test file 2" > /tmp/backer-demo/source/file2.txt
	@mkdir -p /tmp/backer-demo/source/subdir
	@echo "Nested file" > /tmp/backer-demo/source/subdir/nested.txt
	@echo ""
	@echo "Running backup..."
	backer backup /tmp/backer-demo/source /tmp/backer-demo/dest -v
	@echo ""
	@echo "Checking destination:"
	@ls -la /tmp/backer-demo/dest/
	@echo ""
	@echo "Demo complete! Clean up with: rm -rf /tmp/backer-demo"
