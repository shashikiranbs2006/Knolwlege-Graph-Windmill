# Builds and starts everything. Run from PowerShell in this folder:  .\setup.ps1
# Order: farm databases -> farm Ontop endpoints -> hub harvest (SHACL-checked) -> hub + site.
# Add -Neo4j to also start the optional Neo4j farm C and load it into the hub.
param([switch]$Neo4j)
$ErrorActionPreference = 'Stop'
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed" } }

Write-Host '1/5 Farm A (Kelmarsh): preparing data'
uv run --offline python prepare_data.py; Check 'Farm A data preparation'
docker compose up -d --wait db; Check 'Farm A PostgreSQL startup'
docker compose exec -T db psql -q -U vkg -d vkg -v ON_ERROR_STOP=1 -f /db/schema.sql; Check 'Farm A schema'
docker compose exec -T db psql -q -U vkg -d vkg -v ON_ERROR_STOP=1 -f /db/load.sql; Check 'Farm A load'

$profiles = @()
if ((Test-Path 'data/penmanshiel/Penmanshiel_WT_static.csv') -and (Get-ChildItem 'data/penmanshiel/Penmanshiel_SCADA_2016*.zip' -ErrorAction SilentlyContinue)) {
  Write-Host '2/5 Farm B (Penmanshiel): preparing data'
  uv run --offline python prepare_farm_b.py; Check 'Farm B data preparation'
  docker compose --profile uc2 up -d --wait db-b; Check 'Farm B PostgreSQL startup'
  docker compose --profile uc2 exec -T db-b psql -q -U farmb -d farmb -v ON_ERROR_STOP=1 -f /db/schema.sql; Check 'Farm B schema'
  docker compose --profile uc2 exec -T db-b psql -q -U farmb -d farmb -v ON_ERROR_STOP=1 -f /db/load.sql; Check 'Farm B load'
  $profiles += 'uc2'
} else {
  Write-Host '2/5 Farm B skipped: put the Penmanshiel 2016 zips and static CSV in data/penmanshiel to enable use case 2'
}

if ($Neo4j) {
  Write-Host '3/5 Farm C (Neo4j): seeding and exporting'
  docker compose --profile neo4j up -d neo4j; Check 'Neo4j startup'
  $ready = $false
  for ($i = 0; $i -lt 40 -and -not $ready; $i++) { try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:7474 | Out-Null; $ready = $true } catch { Start-Sleep 3 } }
  if (-not $ready) { throw 'Neo4j did not start' }
  uv run --offline python neo4j_farm_c.py --seed; Check 'Neo4j seed'
  uv run --offline python neo4j_farm_c.py; Check 'Neo4j export'
  $profiles += 'neo4j'
} else {
  Write-Host '3/5 Farm C (Neo4j) skipped: run .\setup.ps1 -Neo4j to include it'
  Remove-Item 'build/hub/farm_c.ttl' -ErrorAction SilentlyContinue
}

$composeArgs = @(); foreach ($p in $profiles) { $composeArgs += @("--profile", $p) }
$farmNodes = @('ontop'); if ($profiles -contains 'uc2') { $farmNodes += 'ontop-b' }
Write-Host '4/5 Hub: starting the farm Ontop endpoints, harvesting each farm graph, SHACL check'
docker compose @composeArgs up -d --wait --force-recreate @farmNodes; Check 'Farm Ontop startup'
uv run --with rdflib --with pyshacl python hub/build_hub.py; Check 'Hub build'

Write-Host '5/5 Starting the hub and the site'
docker compose @composeArgs up -d --wait --build --force-recreate --remove-orphans hub site; Check 'Service startup'

Write-Host ''
Write-Host 'Ready:'
Write-Host '  Site (two tabs, one assistant)       http://127.0.0.1:8081/'
Write-Host '  Hub SPARQL endpoint (Fuseki)         http://127.0.0.1:3030/hub/sparql'
Write-Host '  Farm A Ontop endpoint (at the farm)  http://127.0.0.1:8080/sparql'
if ($profiles -contains 'uc2') { Write-Host '  Farm B Ontop endpoint (at the farm)  http://127.0.0.1:8082/sparql' }
Write-Host 'Refresh the hub after farm data changes: uv run --with rdflib --with pyshacl python hub/build_hub.py; docker compose restart hub'
if (-not (Select-String -Path '.env' -Pattern '^LLM_API_KEY=.+' -Quiet -ErrorAction SilentlyContinue)) {
  Write-Host 'Note: no LLM_API_KEY in .env, so the assistant will ask for one. Copy .env.example to .env and add your key.'
}
