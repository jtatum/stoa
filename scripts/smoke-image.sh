#!/usr/bin/env bash
# Exercise the production CMD, migrations, and HTTP server with an isolated DB.
set -euo pipefail

image=${1:?Usage: smoke-image.sh IMAGE}
network="stoa-smoke-$$"
database="$network-db"
application="$network-app"

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    docker logs "$database" 2>/dev/null || true
    docker logs "$application" 2>/dev/null || true
  fi
  docker rm -f "$application" "$database" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

docker run --rm --entrypoint /app/.venv/bin/python "$image" -c '
import importlib.util
import os
import shutil
from pathlib import Path
import stoa

assert os.getuid() == 10001, "runtime must use the unprivileged application UID"
assert not os.access("/app", os.W_OK), "application files must not be writable"
assert shutil.which("uv") is None, "uv belongs only in the builder"
assert importlib.util.find_spec("pip") is None, "pip is not needed at runtime"
assert (Path(stoa.__file__).parent / "templates" / "landing.html").is_file()
'

docker network create "$network" >/dev/null
docker run --detach --name "$database" --network "$network" --network-alias db \
  -e POSTGRES_DB=stoa -e POSTGRES_USER=stoa -e POSTGRES_PASSWORD=smoke-test \
  postgres:17-alpine >/dev/null

ready=false
for _ in {1..30}; do
  if docker exec "$database" pg_isready -U stoa -d stoa >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 2
done
[ "$ready" = true ] || { echo "PostgreSQL did not become ready" >&2; exit 1; }

docker run --detach --name "$application" --network "$network" \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  -e DATABASE_URL=postgresql+asyncpg://stoa:smoke-test@db:5432/stoa \
  -e APP_ENV=production -e SECRET_KEY=smoke-test-only-not-a-production-secret \
  "$image" >/dev/null

for _ in {1..30}; do
  if docker exec "$application" python -c '
import json
from urllib.request import urlopen
with urlopen("http://127.0.0.1:8080/health", timeout=2) as response:
    assert json.load(response) == {"ok": True}
with urlopen("http://127.0.0.1:8080/", timeout=2) as response:
    assert response.status == 200
' 2>/dev/null; then
    echo "Runtime migrations, health check, and template rendering passed."
    exit 0
  fi
  [ "$(docker inspect --format '{{.State.Running}}' "$application")" = true ] || break
  sleep 2
done
echo "Application did not become healthy" >&2
exit 1
