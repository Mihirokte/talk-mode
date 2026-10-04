#!/usr/bin/env bash
# Start the backend and a Cloudflare quick tunnel, then print the skill endpoint URL.
# Usage: scripts/dev.sh            (Ctrl-C stops both)
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "No .env. Run: cp .env.example .env  and fill in OPENROUTER_API_KEY and BRIDGE_SKILL_ID." >&2
  exit 1
fi

env_value() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '"' || true; }
skill_id="$(env_value BRIDGE_SKILL_ID)"
verify="$(env_value BRIDGE_VERIFY_SIGNATURES | tr '[:upper:]' '[:lower:]')"
port="$(env_value BRIDGE_PORT)"; port="${port:-8787}"

# The tunnel makes the endpoint public, so refuse to open it without the two guards.
if [[ -z "$skill_id" ]]; then
  echo "BRIDGE_SKILL_ID is empty in .env. Create the skill first (SETUP.md, 'Create the skill')." >&2
  exit 1
fi
if [[ "$verify" == "false" || "$verify" == "0" || "$verify" == "no" ]]; then
  echo "BRIDGE_VERIFY_SIGNATURES is off in .env; turn it on before exposing the server." >&2
  exit 1
fi
command -v cloudflared >/dev/null || { echo "cloudflared missing: brew install cloudflared" >&2; exit 1; }

mkdir -p data
tunnel_log="data/cloudflared.log"
: > "$tunnel_log"

# The tunnel starts once and keeps its URL; the backend runs in a loop so that
# `make reload` (which stops only the backend) brings it back with fresh code
# and .env, without changing the endpoint address in the Alexa console.
stopping=0
server_pid=""
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$port" >"$tunnel_log" 2>&1 &
tunnel_pid=$!
cleanup() {
  stopping=1
  kill "$server_pid" "$tunnel_pid" 2>/dev/null || true
  rm -f data/server.pid
}
trap cleanup EXIT INT TERM

url=""
for _ in $(seq 1 60); do
  url="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$tunnel_log" | head -1 || true)"
  [[ -n "$url" ]] && break
  sleep 0.5
done
if [[ -z "$url" ]]; then
  echo "Tunnel did not report a URL; see $tunnel_log" >&2
  exit 1
fi

cat <<EOF

  Skill endpoint (HTTPS):  $url/alexa
  SSL certificate type:    "My development endpoint is a sub-domain of a domain
                            that has a wildcard certificate from a certificate authority"

  The URL changes every time this script starts; paste it into the Alexa
  Developer Console > Build > Endpoint each time. Ctrl-C stops everything.
  'make reload' restarts only the backend and keeps this URL.

EOF

while (( ! stopping )); do
  uv run --extra server --extra mcp --env-file .env uvicorn bridge.server:app --host 127.0.0.1 --port "$port" &
  server_pid=$!
  echo "$server_pid" > data/server.pid
  wait "$server_pid" || true
  (( stopping )) && break
  echo "backend stopped; starting it again (tunnel and URL unchanged)"
  sleep 1
done
