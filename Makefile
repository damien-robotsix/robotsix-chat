.PHONY: install test lint format format-check typecheck security clean all

SOURCES = src/robotsix_chat tests

install:
	uv sync --all-extras

test:
	uv run pytest

lint:
	uv run ruff check $(SOURCES)

format:
	uv run ruff check --fix $(SOURCES)
	uv run ruff format $(SOURCES)

format-check:
	uv run ruff format --check $(SOURCES)
	uv run ruff check $(SOURCES)

typecheck:
	uv run mypy $(SOURCES)

security:
	uv run bandit -c pyproject.toml -r src/

clean:
	rm -rf .coverage .mypy_cache .ruff_cache .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

all: lint format-check typecheck test
