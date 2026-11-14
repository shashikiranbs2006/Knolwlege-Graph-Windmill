"""Offline check of the one assistant with a fake OpenAI-compatible LLM (no API key or internet).

Needs the hub (127.0.0.1:3030) and farm A's Ontop endpoint (127.0.0.1:8080) running.
    uv run --offline python test_assistant.py
Checks: prompt grounding per scope, retry after a failed query, farm A tab answers on farm A's
hub graph, cross-farm questions answer across graphs, farm A questions fall back to the farm when the hub
is down, cross-farm questions fail on their own, scope auto-detection, a farm-scoped question naming another farm is refused, synthetic labels, and unsafe or out-of-scope queries.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HUB_UC1 = Path("queries/farm_a_01_fault_diagnosis.rq").read_text(encoding="utf-8")
FARM_UC1 = Path("queries/05_uc1_fault_diagnosis.rq").read_text(encoding="utf-8")
HUB_UC2 = Path("queries/hub_01_cross_farm_fixes.rq").read_text(encoding="utf-8")
REPLY = "Most likely gearbox bearing overheating (confidence 0.7, illustrative demo rule); the last fix was a synthetic demo record."
script, seen = [], []


class FakeLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append((self.headers.get("Authorization"), body))
        data = json.dumps({"choices": [{"message": {"role": "assistant", "content": script.pop(0)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ.update(LLM_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1", LLM_API_KEY="test-key", LLM_MODEL="fake")

import assistant  # noqa: E402  (reads the environment at import)

QUESTION = "What usually causes high gear bearing temperature on a gearbox, and what fixed it last time?"

# 1. Farm A question: a broken query, then the hub query on farm A's graph.
script[:] = ["```sparql\nSELECT ?x WHERE { GRAPH <%s> { ?x wind:noSuchPrefixDeclared ?y } } LIMIT 5\n```" % assistant.FARM_A_GRAPH,
             "```sparql\n" + HUB_UC1 + "\n```", REPLY]
result = assistant.ask(QUESTION, "farm")
system = seen[0][1]["messages"][0]["content"]
assert seen[0][0] == "Bearer test-key" and seen[0][1]["model"] == "fake"
assert "wind:Diagnosis" in system and f"GRAPH <{assistant.FARM_A_GRAPH}>" in system, "farm prompt lacks the ontology or graph scope"
assert len(result["attempts"]) == 1 and "failed" in seen[1][1]["messages"][-1]["content"]
assert result["via"] == "farm" and result["endpoint"] == assistant.HUB
assert result["rows"][0]["fault"] == "Gearbox bearing overheating" and result["rows"][0]["recordKind"] == "synthetic_demo"
assert "synthetic" in result["answer"]
assert result["farm_sparql"] and "GRAPH" not in result["farm_sparql"], "no at-the-farm equivalent"
farm_columns, farm_rows = assistant.run_sparql(result["farm_sparql"], assistant.FARM_A)
assert farm_rows == result["rows"], "farm A's hub graph and farm A's own Ontop endpoint disagree"
print("farm A question: answered on farm A's hub graph; same rows as farm A's own Ontop endpoint")

# 2. Cross-farm question: same assistant, cross-farm prompt and query.
seen.clear()
script[:] = ["```sparql\n" + HUB_UC2 + "\n```", "Each farm fixed gearbox faults differently; these are synthetic demo work orders."]
result = assistant.ask("For gearbox faults, how did each farm fix them and how often?", "cross")
assert "GRAPH ?g" in seen[0][1]["messages"][0]["content"] and "How did each farm fix them" in seen[0][1]["messages"][0]["content"]
assert result["via"] == "cross" and {row["farm"] for row in result["rows"]} >= {"kelmarsh"}
assert all(row["recordKind"] == "synthetic_demo" for row in result["rows"]) and result["farm_sparql"] is None
print(f"cross-farm question: answered across farm graphs {sorted({row['farm'] for row in result['rows']})}")

# 3. The hub is down: farm A questions answer at the farm; cross-farm questions fail on their own.
assistant.HUB = "http://127.0.0.1:9/hub/sparql"
seen.clear()
script[:] = ["```sparql\n" + HUB_UC1 + "\n```", "```sparql\n" + FARM_UC1 + "\n```", REPLY]
result = assistant.ask(QUESTION, "farm")
assert result["via"] == "farm-direct" and result["endpoint"] == assistant.FARM_A and "hub is unavailable" in result["notice"]
assert result["rows"][0]["fault"] == "Gearbox bearing overheating"
try:
    script[:] = ["```sparql\n" + HUB_UC2 + "\n```"]
    assistant.ask("For gearbox faults, how did each farm fix them?", "cross")
    raise AssertionError("cross tab answered without a hub")
except RuntimeError as error:
    assert "hub is unavailable" in str(error)
print("hub down: farm A question answered at the farm; cross-farm question reported the hub as unavailable")

# 4. The LLM is told the answer must mention synthetic data, and the reply is patched if it does not.
script[:] = ["The last fix was re-lubrication."]
assert "synthetic demo data" in assistant.answer_text("q", "s", ["recordKind"], [{"recordKind": "synthetic_demo"}], "farm")

# 4b. Scope auto-detection, and the safety rule for a farm-scoped question that names another farm.
for q, want in (("Kelmarsh 2 raised a high gear bearing temperature alarm. What usually causes it?", "farm"),
                (QUESTION, "farm"), ("For gearbox faults, how did both farms fix them?", "cross"),
                ("What does each farm publish and withhold?", "cross"), ("How does Penmanshiel fix bearing faults?", "cross")):
    assert assistant.detect_scope(q)[0] == want, (q, assistant.detect_scope(q))
seen.clear()
try:
    assistant.ask("How does Penmanshiel fix bearing faults?", "farm")
    raise AssertionError("a farm-scoped question naming another farm was answered")
except ValueError as error:
    assert "another farm" in str(error) and not seen, "refusal must happen before any LLM call"
script[:] = ["```sparql\n" + HUB_UC1 + "\n```", REPLY]
assistant.HUB = "http://127.0.0.1:3030/hub/sparql"
auto = assistant.ask(QUESTION)  # scope defaults to auto
assert auto["scope"] == "farm" and auto["scope_reason"].startswith("detected")
script[:] = ["```sparql\n" + HUB_UC2 + "\n```", "Each farm fixed gearbox faults differently; these are synthetic demo work orders."]
assert assistant.ask("For gearbox faults, how did both farms fix them?")["scope"] == "cross"
print("scope auto-detected; a farm-scoped question naming another farm is refused before the LLM is called")

# 5. Unsafe or out-of-scope queries are refused.
farm_b = "SELECT * WHERE { GRAPH <https://w3id.org/wind-core/data/graph/penmanshiel> { ?s ?p ?o } } LIMIT 5"
for bad, scope in (("DELETE WHERE { ?s ?p ?o }", "farm"), ("SELECT * WHERE { SERVICE <http://evil/> { ?s ?p ?o } }", "cross"),
                   ("SELECT * WHERE { GRAPH <%s> { ?s ?p ?o } } LIMIT 5000" % assistant.FARM_A_GRAPH, "farm"),
                   (farm_b, "farm"), ("SELECT * WHERE { GRAPH ?g { ?s ?p ?o } } LIMIT 5", "farm"),
                   ("SELECT * FROM NAMED <x:y> WHERE { GRAPH ?g { ?s ?p ?o } } LIMIT 5", "cross"),
                   ("SELECT * WHERE { ?s ?p ?o } LIMIT 5", "cross")):
    try:
        assistant.check(bad, scope)
        raise AssertionError(f"accepted unsafe or out-of-scope query for scope {scope}: {bad}")
    except ValueError:
        pass
assistant.check("SELECT ?add WHERE { GRAPH ?g { ?m <https://w3id.org/wind-core/ontology#addresses> ?add } } LIMIT 5", "cross")
print("Assistant passed: grounding per scope, scope detection, retry, hub answers equal the farm, fallback, independent failure, unsafe queries refused.")
