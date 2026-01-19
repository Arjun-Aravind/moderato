# FastLimit Development Makefile

.PHONY: help install dev test lint format clean docker-up docker-down docker-test benchmark commit bump release

# Ensure poetry is in PATH
export PATH := $(HOME)/.local/bin:$(PATH)

# Colors for terminal output
RED := \033[0;31m
GREEN := \033[0;32m
YELLOW := \033[1;33m
NC := \033[0m # No Color

help: ## Show this help message
	@echo "$(GREEN)FastLimit - Rate Limiting Library$(NC)"
	@echo ""
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(YELLOW)%-15s$(NC) %s\n", $$1, $$2}'

install: ## Install dependencies with Poetry
	@echo "$(GREEN)Installing dependencies...$(NC)"
	poetry install

dev: ## Start development environment (Redis only)
	@echo "$(GREEN)Starting development environment...$(NC)"
	docker-compose -f docker-compose.dev.yml up -d
	@echo "$(GREEN)Redis is running on localhost:6379$(NC)"
	@echo "$(GREEN)RedisInsight is available at http://localhost:8001$(NC)"

test: ## Run test suite
	@echo "$(GREEN)Running tests...$(NC)"
	poetry run pytest tests/ -v --tb=short

test-cov: ## Run tests with coverage
	@echo "$(GREEN)Running tests with coverage...$(NC)"
	poetry run pytest tests/ --cov=fastlimit --cov-report=html --cov-report=term

# Individual test categories
test-unit: ## Run unit tests (no Redis required)
	@echo "$(GREEN)Running unit tests...$(NC)"
	poetry run pytest tests/test_utils.py -v

test-fixed-window: ## Run fixed window algorithm tests
	@echo "$(GREEN)Running fixed window tests...$(NC)"
	poetry run pytest tests/test_fixed_window.py -v

test-token-bucket: ## Run token bucket algorithm tests
	@echo "$(GREEN)Running token bucket tests...$(NC)"
	poetry run pytest tests/test_token_bucket.py -v

test-sliding-window: ## Run sliding window algorithm tests
	@echo "$(GREEN)Running sliding window tests...$(NC)"
	poetry run pytest tests/test_sliding_window.py -v

test-concurrency: ## Run concurrency and atomicity tests
	@echo "$(GREEN)Running concurrency tests...$(NC)"
	poetry run pytest tests/test_concurrency.py -v --timeout=60

test-edge-cases: ## Run edge case and error handling tests
	@echo "$(GREEN)Running edge case tests...$(NC)"
	poetry run pytest tests/test_edge_cases.py -v

test-security: ## Run security tests
	@echo "$(GREEN)Running security tests...$(NC)"
	poetry run pytest tests/test_security.py -v

test-algorithms: ## Run all algorithm tests
	@echo "$(GREEN)Running all algorithm tests...$(NC)"
	poetry run pytest tests/test_fixed_window.py tests/test_token_bucket.py tests/test_sliding_window.py -v

test-all: ## Run complete test suite
	@echo "$(GREEN)Running complete test suite...$(NC)"
	poetry run pytest tests/ -v --tb=short

lint: ## Run linting checks
	@echo "$(GREEN)Running linting checks...$(NC)"
	poetry run ruff check fastlimit/ tests/ examples/
	poetry run mypy fastlimit/ --ignore-missing-imports

format: ## Format code with black
	@echo "$(GREEN)Formatting code...$(NC)"
	poetry run black fastlimit/ tests/ examples/
	poetry run ruff check --fix fastlimit/ tests/ examples/

clean: ## Clean up cache and build files
	@echo "$(GREEN)Cleaning up...$(NC)"
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name ".coverage" -delete
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf htmlcov/
	rm -rf dist/
	rm -rf build/
	rm -rf *.egg-info

docker-up: ## Start all services with Docker Compose
	@echo "$(GREEN)Starting Docker services...$(NC)"
	docker-compose up -d
	@echo "$(GREEN)Services started:$(NC)"
	@echo "  - FastAPI Demo: http://localhost:8000"
	@echo "  - Multi-tenant Demo: http://localhost:8001"
	@echo "  - Redis: localhost:6379"

docker-down: ## Stop all Docker services
	@echo "$(YELLOW)Stopping Docker services...$(NC)"
	docker-compose down

docker-test: ## Run tests in Docker
	@echo "$(GREEN)Running tests in Docker...$(NC)"
	docker-compose --profile test run --rm tests

docker-build: ## Build Docker images
	@echo "$(GREEN)Building Docker images...$(NC)"
	docker-compose build

benchmark: ## Run performance benchmark
	@echo "$(GREEN)Running performance benchmark...$(NC)"
	poetry run python benchmarks/performance.py

benchmark-quick: ## Run quick performance benchmark
	@echo "$(GREEN)Running quick performance benchmark...$(NC)"
	poetry run python benchmarks/performance.py --quick

benchmark-docker: ## Run performance benchmark in Docker
	@echo "$(GREEN)Running performance benchmark in Docker...$(NC)"
	docker-compose --profile benchmark run --rm benchmark

ci: test-all benchmark-quick ## Run CI checks (tests + quick benchmark)
	@echo "$(GREEN)CI checks completed$(NC)"

demo: ## Run the algorithms demo
	@echo "$(GREEN)Running algorithms demo...$(NC)"
	poetry run python examples/algorithms_demo.py

run-app: ## Run the example FastAPI app
	@echo "$(GREEN)Starting FastAPI demo app...$(NC)"
	poetry run uvicorn examples.fastapi_app:app --reload --port 8000

run-tenant: ## Run the multi-tenant example
	@echo "$(GREEN)Starting multi-tenant demo...$(NC)"
	poetry run uvicorn examples.multi_tenant:app --reload --port 8001

pre-commit: ## Install pre-commit hooks (includes commit-msg hook)
	@echo "$(GREEN)Installing pre-commit hooks...$(NC)"
	poetry run pre-commit install
	poetry run pre-commit install --hook-type commit-msg

pre-commit-run: ## Run pre-commit on all files
	@echo "$(GREEN)Running pre-commit checks...$(NC)"
	poetry run pre-commit run --all-files

# =============================================================================
# Commitizen / Release Commands
# =============================================================================

commit: ## Interactive conventional commit (use instead of git commit)
	@echo "$(GREEN)Starting interactive commit...$(NC)"
	poetry run cz commit

bump: ## Bump version based on conventional commits
	@echo "$(GREEN)Bumping version...$(NC)"
	poetry run cz bump --changelog
	@echo "$(GREEN)Version bumped! Don't forget to push tags: git push && git push --tags$(NC)"

bump-dry: ## Show what version bump would happen (dry run)
	@echo "$(YELLOW)Dry run - showing what would happen:$(NC)"
	poetry run cz bump --dry-run

release: ## Full release workflow (bump + push)
	@echo "$(YELLOW)This will bump version, update changelog, and push to remote$(NC)"
	@echo "$(RED)Press Ctrl+C to cancel$(NC)"
	@sleep 3
	poetry run cz bump --changelog
	git push origin $$(git branch --show-current)
	git push origin --tags
	@echo "$(GREEN)Release complete!$(NC)"

check-commits: ## Validate commit messages
	@echo "$(GREEN)Checking commit messages...$(NC)"
	poetry run cz check --rev-range HEAD~5..HEAD

publish-test: ## Publish to TestPyPI
	@echo "$(YELLOW)Publishing to TestPyPI...$(NC)"
	poetry config repositories.testpypi https://test.pypi.org/legacy/
	poetry publish --build -r testpypi

publish: ## Publish to PyPI
	@echo "$(RED)Publishing to PyPI...$(NC)"
	@echo "$(RED)Are you sure? Press Ctrl+C to cancel$(NC)"
	@sleep 3
	poetry publish --build

.DEFAULT_GOAL := help
