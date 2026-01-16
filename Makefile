.PHONY: install install-dev setup test test-quick lint format type-check clean build build-agent run-server demo release docker-build docker-up docker-down docker-logs help

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
	@echo "  make build        Build Python distribution packages"
	@echo "  make build-agent  Build Windows agent executable (requires PyInstaller)"
	@echo "  make clean        Remove build artifacts"
	@echo ""
	@echo "Run:"
	@echo "  make run-server   Start the backup server"
	@echo "  make demo         Run a demo backup"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-build Build Docker image locally"
	@echo "  make docker-up    Start Docker container (builds if needed)"
	@echo "  make docker-down  Stop and remove Docker container"
	@echo "  make docker-logs  View Docker container logs"
	@echo ""
	@echo "Release:"
	@echo "  make release VERSION=x.y.z   Create and push a release tag"

# Installation
install:
	pip install -e ".[all]"

install-dev:
	pip install -e ".[dev,all]"
	pip install pyinstaller

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

build-agent:
	@echo "Building Windows agent..."
	@echo "Note: For full Windows build, run on Windows with PyInstaller"
	pip install pyinstaller
	pyinstaller --clean backer-agent.spec

clean:
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	rm -rf htmlcov/ .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Run
run-server:
	backer server start

# Release - creates a git tag and pushes it to trigger GitHub Actions
release:
ifndef VERSION
	$(error VERSION is required. Usage: make release VERSION=0.1.0)
endif
	@echo "Creating release v$(VERSION)..."
	@sed -i 's/version = ".*"/version = "$(VERSION)"/' pyproject.toml
	git add pyproject.toml
	git commit -m "Release v$(VERSION)"
	git tag -a "v$(VERSION)" -m "Release v$(VERSION)"
	@echo ""
	@echo "Release v$(VERSION) created locally."
	@echo "To publish, run: git push && git push --tags"

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

# Docker targets
docker-build:
	@echo "Building Docker image..."
	docker compose build

docker-up:
	@echo "Starting Docker container..."
	docker compose up -d
	@echo ""
	@echo "Backer is starting. Waiting for health check..."
	@sleep 5
	@echo ""
	@echo "Web UI: http://localhost:8420"
	@echo "Default login: admin / admin"
	@echo ""
	@echo "View logs with: make docker-logs"

docker-down:
	@echo "Stopping Docker container..."
	docker compose down

docker-logs:
	docker compose logs -f backer