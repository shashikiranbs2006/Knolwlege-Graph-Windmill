$ErrorActionPreference = 'Stop'

uv run --offline python prepare_data.py
if ($LASTEXITCODE -ne 0) { throw 'Data preparation failed' }

docker compose up -d --wait db
if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL startup failed' }

docker compose exec -T db psql -U vkg -d vkg -v ON_ERROR_STOP=1 -f /db/schema.sql
if ($LASTEXITCODE -ne 0) { throw 'Schema setup failed' }

docker compose exec -T db psql -U vkg -d vkg -v ON_ERROR_STOP=1 -f /db/load.sql
if ($LASTEXITCODE -ne 0) { throw 'Data load failed' }

docker compose up -d --wait ontop workbench
if ($LASTEXITCODE -ne 0) { throw 'Ontop startup failed' }

Write-Host 'V1 data loaded. Open http://127.0.0.1:8081/ to write your own SPARQL query.'
