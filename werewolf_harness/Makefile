# Run from this folder. The package is imported as `werewolf_harness`, so python runs from the parent.
PY ?= ../.venv/bin/python
ROOT := $(abspath ..)

.PHONY: dev run worker test test-core lint migrate bench chaos load up down

dev:            ## Postgres + Redis + Jaeger in Docker
	docker compose -f deploy/docker-compose.dev.yml up -d
run:            ## API on :8402 (reads .env)
	cd $(ROOT) && $(PY) -m uvicorn werewolf_harness.app.api.main:app --port 8402 --reload
worker:         ## a model worker (AGENT_EXECUTOR=streams)
	cd $(ROOT) && $(PY) -m werewolf_harness.app.llm.worker
migrate:
	cd $(ROOT) && $(PY) -m alembic -c werewolf_harness/alembic.ini upgrade head
test:           ## everything (integration tests need make dev)
	cd $(ROOT) && $(PY) -m pytest werewolf_harness/tests -q
test-core:      ## no database needed
	cd $(ROOT) && $(PY) -m pytest werewolf_harness/tests -q --ignore=werewolf_harness/tests/integration
lint:
	ruff check app tests/integration scripts loadtest
bench:
	cd $(ROOT) && $(PY) -m werewolf_harness.scripts.bench_runtime && $(PY) -m werewolf_harness.scripts.bench_llm \
	  && $(PY) -m werewolf_harness.scripts.measure_prompt_cache
chaos:          ## kill -9 a node mid-game and watch the other one adopt it
	cd $(ROOT) && $(PY) -m werewolf_harness.scripts.chaos_demo
load:           ## WebSocket load test against a running server
	cd $(ROOT) && $(PY) -m werewolf_harness.loadtest.bots --tables 200
up:
	docker compose -f deploy/docker-compose.yml up -d --build
down:
	docker compose -f deploy/docker-compose.yml down
