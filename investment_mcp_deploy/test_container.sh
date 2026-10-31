#!/usr/bin/env bash
set -euo pipefail
image="${1:-investment-mcp:test}"
container="investment-mcp-test-$$"
volume="$container-data"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker volume create "$volume" >/dev/null
docker run -d --name "$container" --mount "type=volume,source=$volume,target=/data" \
  -e OAUTH_ISSUER=https://issuer.example/ \
  -e MCP_RESOURCE_URL=https://mcp.example/mcp \
  -e INVESTMENT_OWNER_SUB=owner "$image" >/dev/null
ready() {
  docker exec -i "$container" python - <<'PY'
import time,urllib.request
for attempt in range(30):
    try:
        assert urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2).status==200
        break
    except Exception:
        if attempt==29:raise
        time.sleep(.5)
PY
}
ready
docker exec -i --user 10001:10001 "$container" python - <<'PY'
import pathlib,sqlite3,urllib.error,urllib.request
assert pathlib.Path('/proc/1/status').read_text().split('Uid:\t')[1].split()[0]=='10001'
req=urllib.request.Request('http://127.0.0.1:8000/mcp',data=b'{}',headers={'Content-Type':'application/json'})
try:
    urllib.request.urlopen(req,timeout=3)
    raise AssertionError('unauthenticated request accepted')
except urllib.error.HTTPError as exc:assert exc.code==401
with sqlite3.connect('/data/investment.sqlite') as c:
    c.execute('CREATE TABLE smoke_marker(id INTEGER PRIMARY KEY)')
    c.execute('INSERT INTO smoke_marker VALUES(1)')
PY
docker restart "$container" >/dev/null
ready
docker exec --user 10001:10001 "$container" python -c "import sqlite3; c=sqlite3.connect('/data/investment.sqlite'); assert c.execute('SELECT count(*) FROM smoke_marker').fetchone()[0]==1"
printf '%s\n' 'Docker: nonroot startup, health, unauthorized rejection and restart persistence PASS'
