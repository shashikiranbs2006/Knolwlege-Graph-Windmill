"""Build the hub dataset (build/hub/hub.trig) for Apache Jena Fuseki.

The hub is one SPARQL endpoint with one named graph per farm plus shared curated knowledge:

    default graph                         wind-core ontology, farm registry, fault catalogue,
                                          curated alarm-type -> fault links as RDF 1.2 annotations
    <.../data/graph/kelmarsh>             farm A's published graph (from its Ontop endpoint)
    <.../data/graph/penmanshiel>          farm B's published graph (from its Ontop endpoint)
    <.../data/graph/farm-c>               optional farm C, exported from Neo4j

Each farm keeps its data in its own database and publishes it through its own Ontop
endpoint (virtual, nothing is copied at the farm). The hub *harvests* the farm's published
asset, event, diagnosis, maintenance and dataset records with a SPARQL CONSTRUCT, checks
them against the wind-core SHACL shapes, and only loads a farm's graph if it conforms.
The SCADA time series (sosa:Observation) are not harvested: they stay at the farm and are
queried at the farm's own endpoint.

If a farm endpoint cannot be reached, the last validated snapshot of that farm is kept
(and its harvest time says how old it is), so one farm being down never empties the hub.

    uv run --with rdflib --with pyshacl python hub/build_hub.py            # harvest + build
    uv run --with rdflib --with pyshacl python hub/build_hub.py --offline  # rebuild from snapshots
"""

import argparse
import csv
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA = "https://w3id.org/wind-core/data/"
OUT = Path("build/hub")
SNAPSHOTS = OUT / "farms"

# Everything a farm publishes except the time series, one class at a time (one CONSTRUCT per
# class lets Ontop prune its mappings; a single query over all classes is far slower).
CLASSES = ("wind:WindFarm", "wind:WindTurbine", "wind:Component", "wind:OperationalEvent", "wind:FaultCondition",
           "wind:Diagnosis", "wind:MaintenanceEvent", "wind:Dataset", "wind:Technician", "wind:ReplacementPart",
           "wind:MaintenanceLog", "time:Instant")  # SCADA streams, sensors and observations stay at the farm
HARVEST = """PREFIX wind: <https://w3id.org/wind-core/ontology#>
PREFIX time: <http://www.w3.org/2006/time#>
CONSTRUCT { ?s ?p ?o } WHERE { ?s a %s ; ?p ?o }"""


def rows(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def lit(text):
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def slug(pattern):
    words = re.sub(r"[^A-Za-z0-9]+", " ", pattern).title().replace(" ", "")
    return words or "Alarm"


def graph_iri(farm_id):
    return f"{DATA}graph/{farm_id}"


def harvest(endpoint):
    """CONSTRUCT the farm's published records from its Ontop endpoint, as an rdflib graph."""
    from rdflib import Graph
    data = Graph()
    for cls in CLASSES:
        request = urllib.request.Request(endpoint, data=urllib.parse.urlencode({"query": HARVEST % cls}).encode(),
                                         headers={"Accept": "application/n-triples"})
        with urllib.request.urlopen(request, timeout=600) as response:
            data.parse(data=response.read().decode("utf-8"), format="nt")
    return data


def conforms(data):
    """Validate a harvested farm graph against the wind-core SHACL shapes (RDFS inference on)."""
    from pyshacl import validate
    from rdflib import Graph
    ok, _, report = validate(data, shacl_graph=Graph().parse("vkg/shapes.ttl"), ont_graph=Graph().parse("vkg/ontology.ttl"),
                             inference="rdfs", allow_warnings=True)
    return ok, report


def refresh(farm, offline):
    """Return the farm's snapshot status; writes build/hub/farms/<id>.nt when a fresh harvest conforms."""
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    snapshot, meta_file = SNAPSHOTS / f"{farm['farm_id']}.nt", SNAPSHOTS / f"{farm['farm_id']}.json"
    previous = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() and snapshot.exists() else None
    if offline:
        return previous, "offline rebuild: kept the last validated snapshot" if previous else "offline rebuild: no snapshot yet"
    endpoint = farm["harvest_endpoint"]
    try:
        data = harvest(endpoint)
    except Exception as error:  # endpoint down or refusing: keep the last good snapshot
        return previous, f"could not reach {endpoint} ({error}); " + ("kept the last validated snapshot" if previous else "no snapshot loaded")
    ok, report = conforms(data)
    if not ok:
        (SNAPSHOTS / f"{farm['farm_id']}.shacl-report.txt").write_text(report, encoding="utf-8")
        return previous, f"SHACL validation FAILED (report in {SNAPSHOTS / (farm['farm_id'] + '.shacl-report.txt')}); " + \
            ("kept the last validated snapshot" if previous else "farm not loaded")
    snapshot.write_text(data.serialize(format="nt"), encoding="utf-8")
    meta = {"harvestedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "triples": len(data), "endpoint": endpoint}
    meta_file.write_text(json.dumps(meta), encoding="utf-8")
    return meta, f"harvested {len(data)} triples from {endpoint}; SHACL conforms"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="do not contact the farm endpoints; rebuild from snapshots")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    ontology = Path("vkg/ontology.ttl").read_text(encoding="utf-8")
    prefixes = [line for line in ontology.splitlines() if line.startswith("@prefix")]
    default = [line for line in ontology.splitlines() if not line.startswith("@prefix")]
    prefixes.append("@prefix data: <https://w3id.org/wind-core/data/> .")
    graphs = []

    default.append("# ---------- Farm registry: which named graph and endpoint holds each farm ----------")
    for farm in rows("hub/farms.csv"):
        meta, note = refresh(farm, args.offline)
        print(f"{farm['farm_id']}: {note}")
        iri = f"<{DATA}farm/{farm['farm_id']}>"
        default.append(f"{iri} a wind:WindFarm ; wind:farmId {lit(farm['farm_id'])} ; wind:name {lit(farm['name'])} ; "
                       f"wind:sparqlEndpoint <{farm['endpoint']}> ; wind:operator {lit(farm['operator'])} ; "
                       f"wind:exposes {lit(farm['exposes'])} ; wind:withholds {lit(farm['withholds'])} .")
        if meta:
            default.append(f"{iri} wind:graph <{graph_iri(farm['farm_id'])}> ; wind:harvestedAt {lit(meta['harvestedAt'])}^^xsd:dateTime ; "
                           f"wind:tripleCount {meta['triples']} ; wind:validatedBy <https://w3id.org/wind-core/shapes> .")
            graphs.append((farm["farm_id"], (SNAPSHOTS / f"{farm['farm_id']}.nt").read_text(encoding="utf-8")))

    farm_c = OUT / "farm_c.ttl"
    if farm_c.exists():  # optional Neo4j farm exported by neo4j_farm_c.py (full IRIs, RDF 1.2 annotations)
        stamp = datetime.fromtimestamp(farm_c.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        default.append(f"<{DATA}farm/farm-c> a wind:WindFarm ; wind:farmId \"farm-c\" ; wind:name \"Farm C (Neo4j demo)\" ; "
                       f"wind:operator \"Farm C node (Neo4j export)\" ; wind:withholds \"Technician names\" ; "
                       f"wind:graph <{graph_iri('farm-c')}> ; wind:harvestedAt {lit(stamp)}^^xsd:dateTime .")
        graphs.append(("farm-c", farm_c.read_text(encoding="utf-8")))
        print("farm-c: loaded the Neo4j export (materialised; not SHACL-checked here because it uses RDF 1.2 annotations)")

    default += ["", f"<{DATA}dataset/v11-curated-fault-rules> a wind:Dataset ; wind:recordKind \"curated_demo\" ; "
                "wind:source \"Alarm-to-fault rules written for this demo (data/curated)\" ; wind:ownedBy \"V1 project team\" ; "
                "wind:license \"Not specified\" ; wind:note \"Illustrative expert-style rules; confidence values are not validated\" .",
                "# ---------- Shared fault catalogue ----------"]
    for fault in rows("data/curated/fault_conditions.csv"):
        default.append(f"<{DATA}fault/{fault['fault_code']}> a wind:FaultCondition ; skos:prefLabel {lit(fault['label'])} ; "
                       f"wind:name {lit(fault['label'])} ; skos:definition {lit(fault['description'])} ; "
                       f"wind:typicalComponent wind:{fault['component_type']} ; wind:recordKind \"curated_demo\" .")
    default.append("# ---------- Curated alarm knowledge as RDF 1.2 annotations ----------")
    types = {}
    for rule in rows("data/curated/alarm_fault_rules.csv"):
        iri = f"<{DATA}alarm-type/{slug(rule['message_pattern'])}>"
        if iri not in types:
            types[iri] = rule["message_pattern"]
            default.append(f"{iri} a wind:AlarmType ; wind:messagePattern {lit(rule['message_pattern'])} .")
        default.append(f"{iri} wind:suggestsFault <{DATA}fault/{rule['fault_code']}> {{| wind:confidence {float(rule['confidence'])} ; "
                       f"wind:rule {lit(rule['rule_id'])} ; wind:evidenceNote {lit(rule['rationale'])} ; "
                       f"prov:wasDerivedFrom <{DATA}dataset/v11-curated-fault-rules> |}} .")

    out = prefixes + [""] + default + [""]
    for farm_id, body in graphs:
        out += [f"# ---------- Named graph: farm {farm_id} ----------", f"<{graph_iri(farm_id)}> {{", body.rstrip(), "}", ""]
    (OUT / "hub.trig").write_text("\n".join(out) + "\n", encoding="utf-8")
    (OUT / "hub.ttl").unlink(missing_ok=True)  # replaced by hub.trig
    print(f"Wrote {OUT / 'hub.trig'}: {len(graphs)} farm graph(s) [{', '.join(f for f, _ in graphs)}], "
          f"{len(types)} alarm types, {len(rows('data/curated/alarm_fault_rules.csv'))} annotated links")
    if not graphs:
        sys.exit("No farm graph could be loaded into the hub.")


if __name__ == "__main__":
    main()
