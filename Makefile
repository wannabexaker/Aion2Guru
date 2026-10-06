.PHONY: sync lint fmt typecheck test check testdb-up testdb-down

TEST_DB ?= postgresql://postgres@localhost:55432/postgres

sync:
	uv sync

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

fmt:
	uv run ruff check --fix src tests
	uv run ruff format src tests

typecheck:
	uv run mypy

test:
	GURU_TEST_DATABASE_URL=$${GURU_TEST_DATABASE_URL:-$(TEST_DB)} uv run pytest -q

check: lint typecheck test

# Local throwaway Postgres+pgvector for integration tests (needs Docker).
testdb-up:
	docker run -d --rm --name guru-testdb -e POSTGRES_HOST_AUTH_METHOD=trust -p 55432:5432 pgvector/pgvector:pg16

testdb-down:
	docker stop guru-testdb
