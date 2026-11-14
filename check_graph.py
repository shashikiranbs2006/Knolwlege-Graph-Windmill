"""Check the wind-core ontology and validate a farm node against the SHACL shapes.

Run (needs network once to fetch the libraries):
    uv run --with rdflib --with pyshacl python check_graph.py [endpoint] [mapping.obda]
    e.g. farm B: check_graph.py http://127.0.0.1:8082/sparql farm_b/mapping.obda

1. Parses vkg/ontology.ttl and checks that every class and property used by the
   mapping is declared (catches typos such as a misspelt component class).
2. Pulls all non-time-series entities plus a sample of observations from the
   farm's SPARQL endpoint with CONSTRUCT and validates them with SHACL
   (RDFS inference on, so a wind:Gearbox counts as a wind:Component).
"""

import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from pyshacl import validate
from rdflib import Graph, Namespace, OWL, RDF, RDFS

WIND = Namespace("https://w3id.org/wind-core/ontology#")
ENDPOINT = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080/sparql"
MAPPING = Path(sys.argv[2] if len(sys.argv) > 2 else "vkg/mapping.obda")
VKG = Path("vkg")

ENTITY_QUERY = """
PREFIX wind: <https://w3id.org/wind-core/ontology#>
CONSTRUCT { ?s ?p ?o } WHERE {
  VALUES ?class { wind:WindFarm wind:WindTurbine wind:Component wind:OperationalEvent wind:FaultCondition
                  wind:Diagnosis wind:MaintenanceEvent wind:Dataset wind:Technician wind:ReplacementPart wind:MaintenanceLog }
  ?s a ?class ; ?p ?o .
}"""
EDGE_QUERY = """
PREFIX wind: <https://w3id.org/wind-core/ontology#>
CONSTRUCT { ?t wind:hasComponent ?c } WHERE { ?t wind:hasComponent ?c }"""
SAMPLE_IDS = """
PREFIX sosa: <http://www.w3.org/ns/sosa/>
PREFIX wind: <https://w3id.org/wind-core/ontology#>
SELECT ?o WHERE { ?o a sosa:Observation ; sosa:observedProperty wind:%s } LIMIT %d"""
SAMPLE_QUERY = """CONSTRUCT { ?o ?p ?v } WHERE { VALUES ?o { %s } ?o ?p ?v }"""
SIGNALS = ("Power", "EnergyExport", "WindSpeed", "GearOilTemperature")


def select(query):
    request = urllib.request.Request(
        ENDPOINT, data=urllib.parse.urlencode({"query": query}).encode(), headers={"Accept": "text/csv"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read().decode("utf-8").splitlines()[1:]


def construct(query):
    request = urllib.request.Request(
        ENDPOINT, data=urllib.parse.urlencode({"query": query}).encode(), headers={"Accept": "text/turtle"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return Graph().parse(data=response.read().decode("utf-8"), format="turtle")


def check_ontology():
    ontology = Graph().parse(VKG / "ontology.ttl")
    declared = set(ontology.subjects(RDF.type, OWL.Class)) | set(ontology.subjects(RDF.type, OWL.ObjectProperty)) \
        | set(ontology.subjects(RDF.type, OWL.DatatypeProperty)) | set(ontology.subjects(RDF.type, None))
    used = set(re.findall(r"wind:([A-Za-z]+)", MAPPING.read_text(encoding="utf-8")))
    missing = sorted(name for name in used if WIND[name] not in declared)
    undocumented = sorted(str(s) for s in declared if str(s).startswith(str(WIND)) and not ontology.value(s, RDFS.comment))
    for subclass, superclass in ontology.subject_objects(RDFS.subClassOf):
        if (superclass, RDFS.subClassOf, subclass) in ontology:
            raise SystemExit(f"Subclass cycle: {subclass} <-> {superclass}")
    print(f"Ontology: {len(declared)} declared terms; mapping uses {len(used)} wind: terms.")
    if missing or undocumented:
        raise SystemExit(f"Undeclared terms used by the mapping: {missing}; terms without rdfs:comment: {undocumented}")
    return ontology


def main():
    ontology = check_ontology()
    data = construct(ENTITY_QUERY) + construct(EDGE_QUERY)
    for signal in SIGNALS:
        ids = select(SAMPLE_IDS % (signal, 250))
        if not ids:
            print(f"note: this farm publishes no {signal} observations")
            continue
        data += construct(SAMPLE_QUERY % " ".join(f"<{iri}>" for iri in ids))
    print(f"Fetched {len(data)} triples from {ENDPOINT} for validation.")
    conforms, _, report = validate(
        data, shacl_graph=Graph().parse(VKG / "shapes.ttl"), ont_graph=ontology, inference="rdfs", allow_warnings=True)
    if not conforms:
        print(report)
        raise SystemExit("SHACL validation failed.")
    print("SHACL: the farm node conforms to the wind-core shapes.")


if __name__ == "__main__":
    main()
