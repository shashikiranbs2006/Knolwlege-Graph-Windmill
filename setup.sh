#!/usr/bin/env bash
# Linux/macOS equivalent of setup.ps1.  Usage: ./setup.sh [--neo4j]
set -euo pipefail
cd "$(dirname "$0")"
PY="uv run --offline python"; command -v uv >/dev/null || PY=python3
$PY prepare_data.py
docker compose up -d --wait db
docker compose exec -T db psql -q -U vkg -d vkg -v ON_ERROR_STOP=1 -f /db/schema.sql
docker compose exec -T db psql -q -U vkg -d vkg -v ON_ERROR_STOP=1 -f /db/load.sql
profiles=()
if ls data/penmanshiel/Penmanshiel_SCADA_2016*.zip >/dev/null 2>&1 && [ -f data/penmanshiel/Penmanshiel_WT_static.csv ]; then
  $PY prepare_farm_b.py
  docker compose --profile uc2 up -d --wait db-b
  docker compose --profile uc2 exec -T db-b psql -q -U farmb -d farmb -v ON_ERROR_STOP=1 -f /db/schema.sql
  docker compose --profile uc2 exec -T db-b psql -q -U farmb -d farmb -v ON_ERROR_STOP=1 -f /db/load.sql
  profiles+=(--profile uc2)
fi
if [ "${1:-}" = "--neo4j" ]; then
  docker compose --profile neo4j up -d neo4j
  for _ in $(seq 40); do curl -fs -o /dev/null http://127.0.0.1:7474 && break; sleep 3; done
  $PY neo4j_farm_c.py --seed && $PY neo4j_farm_c.py
  profiles+=(--profile neo4j)
else
  rm -f build/hub/farm_c.ttl
fi
nodes=(ontop); [[ " ${profiles[*]-} " == *" uc2 "* ]] && nodes+=(ontop-b)
docker compose "${profiles[@]}" up -d --wait --force-recreate "${nodes[@]}"
uv run --with rdflib --with pyshacl python hub/build_hub.py   # harvest each farm graph, SHACL check
docker compose "${profiles[@]}" up -d --wait --build --force-recreate --remove-orphans hub site
echo "Ready: http://127.0.0.1:8081/ (one site: farm A tab and all-farms tab); hub at http://127.0.0.1:3030/hub/sparql"
