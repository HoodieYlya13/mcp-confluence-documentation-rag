# ========================================================
# Makefile for MCP Confluence Documentation RAG
# ========================================================

.PHONY: help build test lint run-eval run-agent run-server docker-build docker-eval docker-agent docker-server clean

help:
	@echo "========================================================"
	@echo "  ATS Ops Substrate - Command Shortcuts"
	@echo "========================================================"
	@echo "Local (venv) commands:"
	@echo "  make build          - Initialize venv and install dependencies"
	@echo "  make test           - Run the pytest unit & security test suite"
	@echo "  make lint           - Run ruff static analysis"
	@echo "  make run-eval       - Execute offline evaluation battery"
	@echo "  make run-agent      - Execute multi-turn agent loop demo"
	@echo "  make run-server     - Start FastMCP Server over stdio"
	@echo ""
	@echo "Docker commands:"
	@echo "  make docker-build   - Build multi-stage Docker image"
	@echo "  make docker-eval    - Run evaluation suite container"
	@echo "  make docker-agent   - Run interactive agent loop container"
	@echo "  make docker-server  - Spin up MCP server container"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean          - Remove venv and python cache files"
	@echo "========================================================"

# ------------------------------------------
# Local (Venv) execution targets
# ------------------------------------------

build:
	python3 -m venv venv
	./venv/bin/pip install --upgrade pip
	./venv/bin/pip install -r requirements-dev.txt

build-full:
	./venv/bin/pip install -r requirements-semantic.txt -r requirements-dev.txt

test:
	./venv/bin/python3 -m pytest

test-fast:
	./venv/bin/python3 -m pytest -m "not semantic"

lint:
	./venv/bin/ruff check src tests scripts

run-eval:
	DOCUMENT_SOURCE=local RETRIEVER_BACKEND=tfidf LLM_BACKEND=stub ./venv/bin/python3 -m src.eval_suite

run-eval-full:
	DOCUMENT_SOURCE=local RETRIEVER_BACKEND=semantic LLM_BACKEND=auto ./venv/bin/python3 -m src.eval_suite

run-agent:
	./venv/bin/python3 -m src.agent_loop

run-server:
	./venv/bin/python3 -m src.server

run-server-http:
	MCP_TRANSPORT=streamable-http ./venv/bin/python3 -m src.server

seed:
	./venv/bin/python3 scripts/seed_confluence.py

deploy:
	./venv/bin/python3 scripts/deploy_hf_space.py

# ------------------------------------------
# Docker execution targets
# ------------------------------------------

docker-build:
	docker compose build

docker-eval:
	docker compose run --rm eval-suite

docker-agent:
	docker compose run --rm agent-loop

docker-server:
	docker compose up mcp-server

# ------------------------------------------
# Cleanup target
# ------------------------------------------

clean:
	rm -rf venv
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
