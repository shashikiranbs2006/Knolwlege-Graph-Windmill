"""Integration smoke check for the running local SPARQL workbench."""

import csv
import json
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


def run(query, path="/"):
    request = urllib.request.Request(
        "http://127.0.0.1:8081" + path,
        data=urllib.parse.urlencode({"sparql": query}).encode(),
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


prefix = "PREFIX : <https://example.org/wind-v1/>\n"
status, assets = run(prefix + "SELECT ?name WHERE { ?turbine a :WindTurbine ; :name ?name . } LIMIT 10")
assert status == 200 and "Kelmarsh 1" in assets and "Kelmarsh 6" in assets
assert "PostgreSQL relations: turbines" in assets and 'FROM &quot;turbines&quot;' in assets

status, faults = run(prefix + "SELECT ?name WHERE { ?fault a :FaultCondition ; :name ?name . } LIMIT 10")
assert status == 200 and "Hypothetical" in faults and "Kelmarsh 1" not in faults
assert "PostgreSQL relations: synthetic_maintenance" in faults
assert "fabricated demo cases" in faults and 'FROM &quot;synthetic_maintenance&quot;' in faults

status, error = run("SELECT WHERE { broken }")
assert status == 400 and "role=\"alert\"" in error

cross_query = Path("queries/02_event_join.rq").read_text(encoding="utf-8")
status, content = run(cross_query, "/inspect")
inspection = json.loads(content)
assert status == 200 and ":Power" in [term["token"] for term in inspection["terms"]]
assert {"turbines", "observations", "events"} <= {rule["id"] for rule in inspection["mappings"]}

status, content = run(cross_query, "/reformulate")
trace = json.loads(content)
assert status == 200 and {"turbines", "measurement_observations", "status_events"} <= set(trace["relations"])
assert "scada_readings" in trace["views"]["measurement_observations"]

status, content = run(cross_query, "/answer")
answer = json.loads(content)
assert status == 200 and len(answer["rows"]) == 5
first = answer["rows"][0]
with zipfile.ZipFile("data/kelmarsh/Kelmarsh_SCADA_2016_3082.zip") as archive:
    with archive.open(first["sourceFile"]) as raw:
        lines = (line.decode("utf-8-sig").rstrip("\r\n") for line in raw)
        header = next(line[2:] for line in lines if line.startswith("# Date and time,"))
        columns = next(csv.reader([header]))
        source = next(row for number, row in enumerate(csv.reader(lines), 1) if number == int(first["sourceRow"]))
        assert source[columns.index("Power (kW)")] == first["power"]

changed = cross_query.replace('2016-01-29T12:00:00Z', '2016-01-30T12:00:00Z').replace('2016-01-29T13:00:00Z', '2016-01-30T13:00:00Z')
status, content = run(changed, "/answer")
assert status == 200 and json.loads(content)["rows"] != answer["rows"]
status, content = run(changed, "/reformulate")
assert status == 200 and json.loads(content)["reformulation"] != trace["reformulation"]

status, content = run(prefix + 'SELECT ?name WHERE { ?t a :WindTurbine ; :name ?name . FILTER (?name = "No such turbine") } LIMIT 5', "/answer")
assert status == 200 and json.loads(content)["rows"] == []
status, content = run("SELECT WHERE { broken }", "/reformulate")
assert status == 400 and json.loads(content)["error"]

print("Workbench passed: live paths, source ZIP row, changed and empty results, synthetic warning, invalid query.")
