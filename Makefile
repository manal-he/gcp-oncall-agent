# Developer shortcuts. On Windows without make, run the commands on the right directly.
PY ?= python
VENV ?= .venv
BIN := $(VENV)/bin
ifeq ($(OS),Windows_NT)
  BIN := $(VENV)/Scripts
endif

.PHONY: help install dev api ui test lint fmt typecheck eval eval-injection eval-budget eval-llm mcp mcp-http docker up clean

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-16s %s\n", $$1, $$2}'

install: ## Create .venv and install the package with dev extras, plus UI deps
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install -U pip
	$(BIN)/pip install -e ".[dev]"
	cd ui && npm ci

dev: ## API on :8080 and Vite dev server on :5173 (Ctrl-C stops both)
	$(MAKE) -j2 api ui

api: ## FastAPI service (simulated backend + mock LLM by default)
	$(BIN)/oncall serve --port 8080

ui: ## Vite dev server with proxy to the API
	cd ui && npm run dev

test: ## Offline test suite
	$(BIN)/pytest -q

lint: ## Ruff lint + format check + mypy
	$(BIN)/ruff check src tests
	$(BIN)/ruff format --check src tests
	$(BIN)/mypy src

fmt: ## Auto-format
	$(BIN)/ruff format src tests
	$(BIN)/ruff check --fix src tests

typecheck: ## mypy + tsc
	$(BIN)/mypy src
	cd ui && npm run typecheck

eval: ## Eval suite with the scripted mock (harness validation; offline)
	$(BIN)/oncall eval --llm mock --out evals/reports/mock-baseline.md

eval-injection: ## Eval with a mock that obeys injected instructions (guardrail test)
	$(BIN)/oncall eval --llm mock-injectable --out evals/reports/mock-injectable.md

eval-budget: ## Eval with a tight tool budget (shows budget enforcement)
	ONCALL_MAX_TOOL_CALLS=3 $(BIN)/oncall eval --llm mock --out evals/reports/mock-budget3.md

eval-llm: ## Eval with a real model, e.g. make eval-llm LLM=vertex (needs credentials, costs money)
	$(BIN)/oncall eval --llm $(LLM) --judge rubric --out evals/reports/$(LLM).md

mcp: ## MCP server over stdio (what MCP clients launch)
	$(BIN)/oncall mcp --transport stdio

mcp-http: ## MCP server over streamable HTTP on 127.0.0.1:8765/mcp
	$(BIN)/oncall mcp --transport http --port 8765

docker: ## Build the container image
	docker build -t gcp-oncall-agent:local .

up: ## docker compose stack (API+UI on :8080, MCP on :8765)
	docker compose up --build

clean: ## Remove caches and build outputs
	rm -rf .pytest_cache .mypy_cache .ruff_cache ui/dist build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
