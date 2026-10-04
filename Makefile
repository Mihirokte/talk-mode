.PHONY: setup check serve replay-serve dev up down status reload tools find-note costs replay deploy invoke destroy

PORT ?= 8787
QUERY ?= Tasks

setup:            ## install deps, enable the secret-scanning git hooks, create local config files
	uv sync --all-extras
	git config core.hooksPath .githooks
	@test -f .env || (cp .env.example .env && chmod 600 .env && echo "created .env: fill in OPENROUTER_API_KEY and BRIDGE_SKILL_ID")
	@test -f mcp_servers.json || (cp mcp_servers.example.json mcp_servers.json && echo "created mcp_servers.json from the example")

check:            ## lint, types, tests and the secret scan over every tracked file
	uv run --extra dev ruff check .
	uv run --all-extras mypy
	uv run --extra dev --extra mcp pytest -q
	python3 scripts/secret_scan.py --tracked

serve:            ## backend only, signature verification as set in .env
	uv run --extra server --extra mcp --env-file .env uvicorn bridge.server:app --host 127.0.0.1 --port $(PORT)

replay-serve:     ## backend for scripts/replay.py: unsigned requests, localhost only, no tunnel
	BRIDGE_VERIFY_SIGNATURES=false uv run --extra server --extra mcp --env-file .env uvicorn bridge.server:app --host 127.0.0.1 --port $(PORT)

dev:              ## backend + Cloudflare quick tunnel; prints the skill endpoint URL
	scripts/dev.sh

up:               ## same as dev, but detached: keeps running after the terminal closes
	python3 scripts/run_detached.py start

down:             ## stop the detached server and tunnel
	python3 scripts/run_detached.py stop

status:           ## is the detached server running, and its endpoint URL
	python3 scripts/run_detached.py status

reload:           ## restart only the detached backend (new code/.env); endpoint URL unchanged
	python3 scripts/run_detached.py reload

tools:            ## start the MCP servers and list the tools the model is offered (free)
	uv run --extra mcp --env-file .env python scripts/mcp_probe.py

find-note:        ## find a Simplenote note id by text, e.g. make find-note QUERY=Tasks (free)
	uv run --extra mcp --env-file .env python scripts/mcp_probe.py --raw simplenote search_notes '{"query": "$(QUERY)", "limit": 5}'

costs:            ## price of every request today, from OpenRouter's own figures
	uv run --no-project --env-file .env python scripts/costs.py

replay:           ## scripted conversation against the local server
	uv run --env-file .env python scripts/replay.py launch "why is the sky blue" "and at sunset?" stop

deploy:           ## build and deploy to AWS Lambda (needs AWS credentials)
	uv run --extra deploy --env-file .env python deploy/deploy.py deploy

invoke:           ## scripted conversation against the deployed Lambda
	uv run --extra deploy --env-file .env python scripts/replay.py --lambda talk-mode launch "what's the date today" stop

destroy:          ## delete the AWS stack and the stored key
	uv run --extra deploy --env-file .env python deploy/deploy.py destroy
