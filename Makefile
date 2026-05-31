.PHONY: check lint format test security install-dev

# Run the full local CI equivalent — same checks as GitHub Actions
check: lint format security test

lint:
	ruff check postmind/

format:
	ruff format --check postmind/ tests/

security:
	bandit -r postmind/ -ll -q

test:
	python -m pytest tests/ -q --tb=short

# Fix lint and format issues in place (use before committing)
fix:
	ruff check postmind/ --fix
	ruff format postmind/ tests/

# Install all dev dependencies + pre-commit hooks
install-dev:
	pip install -e ".[dev]"
	pre-commit install
	@echo "Done. pre-commit will now run on every git commit."
