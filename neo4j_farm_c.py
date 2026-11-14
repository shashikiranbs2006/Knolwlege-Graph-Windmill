"""Optional farm C: bring an existing Neo4j (LPG) knowledge graph into the shared wind-core graph.

Ontop cannot read Neo4j, so this is the honest path for LPG data: a small, declarative-style
export that maps labels and relationship types to wind-core terms and turns relationship
properties (confidence, source) into RDF 1.2 annotations. The result, build/hub/farm_c.ttl,
is loaded into the hub next to the curated knowledge (materialised, not virtual).

    docker compose --profile neo4j up -d neo4j
    python neo4j_farm_c.py --seed      # load the demo LPG (neo4j/farm_c.cypher) into Neo4j
    python neo4j_farm_c.py             # export to build/hub/farm_c.ttl, then rebuild the hub

Governance: technician names stay in Neo4j; only the fact that a technician did the work is exported.
"""

import base64
import csv
import json
import os
import sys
import urllib.request
from pathlib import Path

URL = os.getenv("NEO4J_HTTP", "http://127.0.0.1:7474") + "/db/neo4j/tx/commit"
AUTH = "Basic " + base64.b64encode(f"neo4j:{os.getenv('NEO4J_PASSWORD', 'farmc_demo_local')}".encode()).decode()
DATA = "https://w3id.org/wind-core/data/"
WIND = "https://w3id.org/wind-core/ontology#"
FARM = "farm-c"


def cypher(statement):
    request = urllib.request.Request(URL, data=json.dumps({"statements": [{"statement": statement}]}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": AUTH})
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.load(response)
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    result = body["results"][0]
    return [dict(zip(result["columns"], row["row"])) for row in result["data"]]


def lit(value):
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def main():
    if "--seed" in sys.argv:
        script = "\n".join(line for line in Path("neo4j/farm_c.cypher").read_text(encoding="utf-8").splitlines()
                           if not line.strip().startswith("//"))
        for statement in script.split(";"):
            if statement.strip():
                cypher(statement.strip())
        print("Seeded farm C in Neo4j.")
        return
    with Path("neo4j/fault_names.csv").open(encoding="utf-8") as stream:
        fault_code = {row["neo4j_fault_name"]: row["fault_code"] for row in csv.DictReader(stream)}
    farm = f"<{DATA}farm/{FARM}>"
    turbine = lambda tid: f"<{DATA}farm/{FARM}/turbine/{tid}>"
    gearbox = lambda tid: f"<{DATA}farm/{FARM}/turbine/{tid}/component/Gearbox>"
    out = [f"{farm} a <{WIND}WindFarm> ; <{WIND}farmId> {lit(FARM)} ; <{WIND}name> {lit('Farm C (Neo4j demo)')} ; "
           f"<{WIND}operator> {lit('Farm C node (Neo4j export)')} ; <{WIND}withholds> {lit('Technician names')} ; "
           f"<{WIND}exposes> {lit('WindTurbine, Component, OperationalEvent, MaintenanceEvent')} ; "
           f"<http://www.w3.org/ns/prov#wasDerivedFrom> <{DATA}dataset/farm-c-neo4j-demo> .",
           f"<{DATA}dataset/farm-c-neo4j-demo> a <{WIND}Dataset> ; <{WIND}recordKind> \"synthetic_demo\" ; "
           f"<{WIND}source> \"Neo4j demo graph (neo4j/farm_c.cypher)\" ; <{WIND}ownedBy> \"V1 project team\" ; "
           f"<{WIND}license> \"Not specified\" ; <{WIND}note> \"Fictional LPG example from the team lead's use case 1\" ."]
    for row in cypher("MATCH (t:WindTurbine)-[:HAS_COMPONENT]->(g:Gearbox) RETURN t.id AS t, t.capacity_MW AS mw"):
        out.append(f"{turbine(row['t'])} a <{WIND}WindTurbine> ; <{WIND}name> {lit(row['t'])} ; <{WIND}locatedAt> {farm} ; "
                   f"<{WIND}ratedPowerKW> {row['mw'] * 1000} ; <{WIND}hasComponent> {gearbox(row['t'])} ; <{WIND}recordKind> \"synthetic_demo\" .")
        out.append(f"{gearbox(row['t'])} a <{WIND}Gearbox> ; <{WIND}name> \"Gearbox\" ; <{WIND}recordKind> \"synthetic_demo\" .")
    for row in cypher("MATCH (a:OperationalEvent)-[:RAISED_BY]->(t:WindTurbine) RETURN a.id AS a, a.type AS type, a.at AS at, t.id AS t"):
        event = f"<{DATA}farm/{FARM}/turbine/{row['t']}/event/{row['a']}>"
        out.append(f"{event} a <{WIND}OperationalEvent> ; <{WIND}message> {lit(row['type'])} ; <{WIND}aboutTurbine> {turbine(row['t'])} ; "
                   f"<{WIND}startedAt> \"{row['at']}\"^^<http://www.w3.org/2001/XMLSchema#dateTime> ; <{WIND}recordKind> \"synthetic_demo\" .")
    for row in cypher("MATCH (a:OperationalEvent)-[r:INDICATES_FAULT]->(f:FaultCondition), (a)-[:RAISED_BY]->(t) "
                      "RETURN a.id AS a, t.id AS t, f.name AS f, r.confidence AS c, r.source AS s"):
        event = f"<{DATA}farm/{FARM}/turbine/{row['t']}/event/{row['a']}>"
        out.append(f"{event} <{WIND}indicatesFault> <{DATA}fault/{fault_code[row['f']]}> {{| <{WIND}confidence> {row['c']} ; "
                   f"<{WIND}evidenceNote> {lit('Neo4j relationship property; source ' + row['s'])} |}} .")
    for row in cypher("MATCH (m:MaintenanceEvent)-[:PERFORMED_ON]->(g:Gearbox)<-[:HAS_COMPONENT]-(t), (m)-[:ADDRESSES]->(f) "
                      "RETURN m.id AS m, m.action AS action, m.date AS date, t.id AS t, f.name AS f"):
        out.append(f"<{DATA}farm/{FARM}/maintenance/{row['m']}> a <{WIND}MaintenanceEvent> ; <{WIND}action> {lit(row['action'])} ; "
                   f"<{WIND}startedAt> \"{row['date']}T00:00:00Z\"^^<http://www.w3.org/2001/XMLSchema#dateTime> ; "
                   f"<{WIND}performedOn> {gearbox(row['t'])} ; <{WIND}addresses> <{DATA}fault/{fault_code[row['f']]}> ; "
                   f"<{WIND}recordKind> \"synthetic_demo\" ; <http://www.w3.org/ns/prov#wasDerivedFrom> <{DATA}dataset/farm-c-neo4j-demo> .")
    Path("build/hub").mkdir(parents=True, exist_ok=True)
    Path("build/hub/farm_c.ttl").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"Exported farm C from Neo4j: {len(out)} statements -> build/hub/farm_c.ttl (run hub/build_hub.py next)")


if __name__ == "__main__":
    main()
