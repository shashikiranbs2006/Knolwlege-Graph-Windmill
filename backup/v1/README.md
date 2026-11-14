# Wind energy knowledge graph V1

A local engineer demonstration over Cubico's 2016 Kelmarsh wind-farm data. PostgreSQL stores six turbines, SCADA readings, status events, and two **synthetic** maintenance cases. Ontop 5.5.0 exposes a virtual RDF graph at `http://127.0.0.1:8080/sparql`; no RDF copy is stored. The source files and their licence are described in [data/kelmarsh/README.md](data/kelmarsh/README.md).

## Run

Prerequisites: Docker Desktop running, `uv` with Python 3, and the official [pgJDBC 42.7.14](https://jdbc.postgresql.org/download/) JAR at `cli/jdbc/postgresql-42.7.14.jar`. The JAR is present in this workspace. If it is missing, download it from `https://jdbc.postgresql.org/download/postgresql-42.7.14.jar`. The 2016 ZIP and two small Cubico CSVs must be in `data/kelmarsh/`.

From PowerShell in this directory:

```powershell
.\setup.ps1
```

`setup.ps1` verifies the source hashes, prepares only selected SCADA columns, creates the dedicated `vkg` database tables, loads the archive, and starts Ontop and the query workbench. Re-running it replaces **only the data in these V1 demo tables**. It does not modify the original files. PostgreSQL listens on `127.0.0.1:5433`; Ontop listens on `127.0.0.1:8080`; the workbench listens on `127.0.0.1:8081`. Stop services with `docker compose stop`. Do not use `docker compose down -v` if you want to keep the imported database.

Open **http://127.0.0.1:8081/** and type your own SPARQL `SELECT` query. The box starts blank. For example:

```sparql
PREFIX : <https://example.org/wind-v1/>
SELECT ?turbine ?name WHERE {
  ?turbine a :WindTurbine ; :name ?name .
} LIMIT 10
```

The answer is fetched live from Ontop. Below the query, **Live query journey** lights up as each real step finishes: graph terms found in the query, matching rules read from the [runtime R2RML mapping](vkg/mapping.ttl), Ontop's returned reformulation, PostgreSQL relations named in its SQL, and the answer. Click a stage to see its evidence. **Replay journey** and **Next step** walk through the completed live result at presentation speed; replay does not rerun the database query. A mapping rule may match a generic term such as `:name` without being the exact rule Ontop selected; the workbench labels these as matches, while the SQL is the evidence for actual relations used.

`measurement_observations` is a PostgreSQL view over `scada_readings`, and the view stage shows its V1 schema definition. Exact CSV source file and row can be reported when your query returns `?sourceFile` and `?sourceRow`; without them the workbench does not claim an exact source row. Queries using `synthetic_maintenance` are flagged as fabricated demo data even if they omit `?recordKind`. The workbench accepts bounded `SELECT` queries only (maximum 100 rows) and runs only on this machine.

For a presentation, paste the editable real-data join in [queries/02_event_join.rq](queries/02_event_join.rq). It connects turbine details, power observations, and a status event. Change its turbine or time condition and run it again to show the SQL and results changing. The three paired queries in `queries/` are also regression checks. Run `uv run --offline python test_workbench.py` to check the live trace phases, a source ZIP row, changed and empty results, the synthetic warning, and invalid-query handling. Run `uv run --offline python run_demo.py --verify --benchmark` to compare example SPARQL answers with SQL and measure a bounded query.

An observed example from the loaded data: the first power reading in the examples is Kelmarsh 2 at 2016-01-29 12:00 UTC, 2053.7099609375 kW, from turbine data row 3817. The overlapping status event is status row 72, "High temp. gear bearing 2". The linked diagnosis and work order are **fabricated demonstration data**, marked `synthetic_demo` with confidence `0.0`; the status event itself is real. The live explanation repeats this distinction under each result table.

To see a trace back to a real source row in the workbench, try this query and change the turbine or power threshold yourself:

```sparql
PREFIX : <https://example.org/wind-v1/>
PREFIX sosa: <http://www.w3.org/ns/sosa/>
SELECT ?sourceFile ?sourceRow ?power WHERE {
  ?reading a sosa:Observation ;
    sosa:hasFeatureOfInterest <https://example.org/wind-v1/turbine/KWF2> ;
    sosa:observedProperty :Power ;
    sosa:hasSimpleResult ?power ;
    :sourceFile ?sourceFile ; :sourceRow ?sourceRow .
  FILTER (?power > 1000)
} LIMIT 1
```

## Data and graph model

The [schema](db/schema.sql) keeps source file and one-based row number for each reading and event, converts source timestamps to UTC, and leaves `NaN` or unknown event ends as SQL `NULL`. The original archive contains 314,496 SCADA rows and 14,019 events; 4,749 events have no reported end. The graph maps three selected signals as SOSA observations: `Power` in [QUDT kilowatts](http://qudt.org/vocab/unit/KiloW), `EnergyExport` in [kilowatt hours](http://qudt.org/vocab/unit/KiloW-HR), and `WindSpeed` in [metres per second](http://qudt.org/vocab/unit/M-PER-SEC). Other selected SCADA values remain in PostgreSQL for later mappings. The corrected unit decisions are in `data/kelmarsh/v1_unit_decisions.csv`.

The [ontology](vkg/ontology.ttl) documents the PDF's asset, operational, and maintenance classes and the five core relationships. It reuses SOSA, PROV-O, OWL-Time, and QUDT identifiers where appropriate, without importing their full ontologies into Ontop's lightweight reasoning profile. The authored [Ontop mapping](vkg/mapping.obda) is converted into the [R2RML mapping](vkg/mapping.ttl) used at runtime. After changing `mapping.obda`, regenerate R2RML with `cli/ontop.bat mapping to-r2rml -i vkg/mapping.obda -o vkg/mapping.ttl --force`, then restart Ontop.

Component identities, fault diagnoses, maintenance activities, technicians, logs, and the one replacement part in V1 are synthetic. `recordKind` and dataset provenance remain queryable, and the example join displays the synthetic label. Logical sensor resources represent SCADA channels, not independently verified physical sensor assets. RotorBlade is documented but unpopulated because the source has no individual blade register.

## Verification and governance

The loaded database was checked for six turbines, 314,496 readings, 14,019 events, and two synthetic cases; 28,803 missing power values remained SQL `NULL`. Ontop's `validate` command completed, and all three sample SPARQL outputs matched their SQL equivalents. A bounded one-day KWF2 power count returned 144 readings; the latest warm query times on this machine were 0.032, 0.006, and 0.005 seconds. This is a local measurement, not a guarantee for other queries or machines.

The dataset metadata records Cubico's source, publication date, ownership, CC BY 4.0 licence, and usage note; the synthetic dataset has its own provenance and a demo-only warning. Keep both ports bound to loopback. If remote access is later needed, place Ontop behind an authenticated proxy, use a read-only PostgreSQL account, and decide authorization by dataset before exposing it. The current local `vkg` database user is an administrator for repeatable imports, so this setup is not a remote deployment configuration.

This V1 demonstrates query joins and provenance. It does not establish actual repair actions, technician identities, replacement parts, or fault causes, and it does not test multiple farms, full nine-year history, or OWL reasoning beyond Ontop's supported profile.
