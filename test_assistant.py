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
HUBR = '{"tool": "hub"}'
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
os.environ.update(FARM_ENDPOINT_COLUMN="harvest_endpoint")  # the registry column that works from the host
os.environ.update(LLM_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1", LLM_API_KEY="test-key", LLM_MODEL="fake")

import assistant  # noqa: E402  (reads the environment at import)

QUESTION = "What usually causes high gear bearing temperature on a gearbox, and what fixed it last time?"

# 1. Farm A question: a broken query, then the hub query on farm A's graph.
script[:] = [HUBR, "```sparql\nSELECT ?x WHERE { GRAPH <%s> { ?x wind:noSuchPrefixDeclared ?y } } LIMIT 5\n```" % assistant.FARM_A_GRAPH,
             "```sparql\n" + HUB_UC1 + "\n```", REPLY]
result = assistant.ask(QUESTION, "farm")
system = seen[1][1]["messages"][0]["content"]
assert seen[1][0] == "Bearer test-key" and seen[1][1]["model"] == "fake"
assert "wind:Diagnosis" in system and f"GRAPH <{assistant.FARM_A_GRAPH}>" in system, "farm prompt lacks the ontology or graph scope"
assert len(result["attempts"]) == 1 and "failed" in seen[2][1]["messages"][-1]["content"] and result["tool"] == "hub"
assert result["via"] == "farm" and result["endpoint"] == assistant.HUB
assert result["rows"][0]["fault"] == "Gearbox bearing overheating" and result["rows"][0]["recordKind"] == "synthetic_demo"
assert "synthetic" in result["answer"]
assert result["farm_sparql"] and "GRAPH" not in result["farm_sparql"], "no at-the-farm equivalent"
farm_columns, farm_rows = assistant.run_sparql(result["farm_sparql"], assistant.FARM_A)
assert farm_rows == result["rows"], "farm A's hub graph and farm A's own Ontop endpoint disagree"
print("farm A question: answered on farm A's hub graph; same rows as farm A's own Ontop endpoint")

# 2. Cross-farm question: same assistant, cross-farm prompt and query.
seen.clear()
script[:] = [HUBR, "```sparql\n" + HUB_UC2 + "\n```", "Each farm fixed gearbox faults differently; these are synthetic demo work orders."]
result = assistant.ask("For gearbox faults, how did each farm fix them and how often?", "cross")
assert "GRAPH ?g" in seen[1][1]["messages"][0]["content"] and "How did each farm fix them" in seen[1][1]["messages"][0]["content"]
assert result["via"] == "cross" and {row["farm"] for row in result["rows"]} >= {"kelmarsh"}
assert all(row["recordKind"] == "synthetic_demo" for row in result["rows"]) and result["farm_sparql"] is None
print(f"cross-farm question: answered across farm graphs {sorted({row['farm'] for row in result['rows']})}")

# 3. The hub is down: farm A questions answer at the farm; cross-farm questions fail on their own.
assistant.HUB = "http://127.0.0.1:9/hub/sparql"
seen.clear()
script[:] = [HUBR, "```sparql\n" + HUB_UC1 + "\n```", "```sparql\n" + FARM_UC1 + "\n```", REPLY]
result = assistant.ask(QUESTION, "farm")
assert result["via"] == "farm-direct" and result["endpoint"] == assistant.FARM_A and "hub is unavailable" in result["notice"]
assert result["rows"][0]["fault"] == "Gearbox bearing overheating"
try:
    script[:] = [HUBR, "```sparql\n" + HUB_UC2 + "\n```"]
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
script[:] = [HUBR, "```sparql\n" + HUB_UC1 + "\n```", REPLY]
assistant.HUB = "http://127.0.0.1:3030/hub/sparql"
auto = assistant.ask(QUESTION)  # scope defaults to auto
assert auto["scope"] == "farm" and auto["scope_reason"].startswith("detected")
script[:] = [HUBR, "```sparql\n" + HUB_UC2 + "\n```", "Each farm fixed gearbox faults differently; these are synthetic demo work orders."]
assert assistant.ask("For gearbox faults, how did both farms fix them?")["scope"] == "cross"
print("scope auto-detected; a farm-scoped question naming another farm is refused before the LLM is called")

# 4c. The farm_metric tool: the LLM only picks parameters; the code asks each farm live.
import farm_metrics  # noqa: E402

POWER = '{"tool": "farm_metric", "metric": "Power", "agg": "avg", "group_by": "farm", "farms": ["kelmarsh", "penmanshiel"]}'
seen.clear()
script[:] = [POWER, "Penmanshiel has the higher average power."]
result = assistant.ask("What is the average power per farm, and which farm is highest?")
assert result["tool"] == "farm_metric" and result["via"] == "farm-live" and result["scope"] == "cross"
values = {row["farm"]: row for row in result["rows"]}
assert abs(values["kelmarsh"]["value"] - 515.0) < 1 and abs(values["penmanshiel"]["value"] - 527.3) < 1, values
assert values["kelmarsh"]["unit"] == "kW" and 0.2 < values["kelmarsh"]["capacity_factor"] < 0.3
assert "indicative" in result["notice"] and "Capacity factor" in result["notice"], "ranking caveat missing"
for farm_id, endpoint in (("kelmarsh", assistant.FARM_A), ("penmanshiel", "http://127.0.0.1:8082/sparql")):  # equals asking the farm directly
    direct = assistant.run_sparql("PREFIX wind: <https://w3id.org/wind-core/ontology#> PREFIX sosa: <http://www.w3.org/ns/sosa/> SELECT (AVG(?v) AS ?a) WHERE { ?o sosa:observedProperty wind:Power ; sosa:hasSimpleResult ?v }", endpoint)[1]
    assert abs(float(direct[0]["a"]) - values[farm_id]["value"]) < 0.01
assert len(seen) == 2, "one routing call and one answer call; the LLM wrote no SPARQL"
print("farm_metric: average power per farm answered live (Kelmarsh %.1f kW, Penmanshiel %.1f kW) = asking each farm directly" % (values["kelmarsh"]["value"], values["penmanshiel"]["value"]))

# Scope: a "This farm" question can only ever touch farm A, even if the LLM asks for both.
script[:] = [POWER, "Kelmarsh averages 515 kW."]
result = assistant.ask("What is the average power?", "farm")
assert {row["farm"] for row in result["rows"]} == {"kelmarsh"} and result["farm_metric"]["params"]["farms"] == ["kelmarsh"]
print("farm_metric: a farm-scoped question never reaches other farms")

# A farm that is offline is named, and the others still answer.
real_registry = farm_metrics.registry


def broken(only=None):
    farms = real_registry()
    for farm_id, farm in farms.items():
        if only is None or farm_id == only:
            farm["harvest_endpoint"] = "http://127.0.0.1:9/sparql"
    return farms


farm_metrics.registry = lambda: broken("penmanshiel")
script[:] = [POWER, "Kelmarsh averages 515 kW."]
result = assistant.ask("Average power per farm, which is highest?")
assert [row["farm"] for row in result["rows"]] == ["kelmarsh"] and result["farm_metric"]["missing"][0]["farm"] == "penmanshiel"
assert "Penmanshiel" in result["answer"] and "Penmanshiel" in result["notice"]
farm_metrics.registry = lambda: broken()
script[:] = [POWER]
result = assistant.ask("Average power per farm, which is highest?")
farm_metrics.registry = real_registry
assert result["rows"] == [] and "Kelmarsh" in result["answer"] and "Penmanshiel" in result["answer"]
print("farm_metric: an offline farm is named in the answer; the other farm still answers; all offline says so")

# Registry rules: unpublished and withheld signals, bad parameters, month grouping and window.
out = farm_metrics.farm_metric({"metric": "GearOilTemperature"})
assert [r["farm"] for r in out["rows"]] == ["kelmarsh"] and "does not publish" in out["missing"][0]["detail"]
reg = real_registry()
reg["kelmarsh"]["withheld_list"] = ["WindSpeed"]
out = farm_metrics.farm_metric({"metric": "WindSpeed", "farms": ["kelmarsh"]}, reg)
assert out["rows"] == [] and "withholds" in out["missing"][0]["detail"]
for bad in ({"metric": "Power; DROP"}, {"metric": "Power", "agg": "median"}, {"metric": "Power", "farms": ["evil"]}, {"metric": "Power", "start": "x"}):
    try:
        farm_metrics.farm_metric(bad)
        raise AssertionError(f"accepted {bad}")
    except ValueError:
        pass
months = farm_metrics.farm_metric({"metric": "WindSpeed", "group_by": "month", "start": "2016-07-01", "end": "2016-09-01"})
assert {r["month"] for r in months["rows"]} == {"2016-07", "2016-08"} and {r["farm"] for r in months["rows"]} == {"kelmarsh", "penmanshiel"}
print("farm_metric: unpublished or withheld signals reported, bad parameters refused, month grouping and window work")
script[:] = ['{"tool": "hub"}', "```sparql\n" + HUB_UC1 + "\n```", REPLY]
assert assistant.ask(QUESTION)["tool"] == "hub", "a knowledge question stays on the hub"


# The model answers the question itself instead of choosing a tool: it is sent back, then the tool runs.
script[:] = ['{"kelmarsh": 3.2, "highest_farm": "kelmarsh"}', POWER, "Penmanshiel has the higher average power."]
result = assistant.ask("What is the average power per farm, and which farm is highest?")
assert result["tool"] == "farm_metric", "a reply that is not a tool choice must be retried, not sent to the hub"
print("routing: a reply that answers the question instead of choosing a tool is retried")

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
# Key rotation: a 429 on the first key moves to the backup; all keys failing gives one clear message.
import urllib.error  # noqa: E402
import io  # noqa: E402

real_urlopen = assistant.urllib.request.urlopen
used = []


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_urlopen(request, timeout=None):
    key = request.get_header("Authorization").split()[-1]
    used.append(key)
    if key in bad_keys:
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"{}"))
    return _Resp(json.dumps({"choices": [{"message": {"content": "ok-" + key}}]}).encode())


os.environ.update(LLM_API_KEY="k-single", LLM_API_KEYS="k-a, k-b")
assert assistant.api_keys() == ["k-a", "k-b", "k-single"]
assistant.urllib.request.urlopen = fake_urlopen
assistant.time.sleep = lambda *_: None
try:
    bad_keys = {"k-a"}
    assert assistant.chat([{"role": "user", "content": "hi"}]) == "ok-k-b" and used == ["k-a", "k-b"]
    used.clear()
    assert assistant.chat([{"role": "user", "content": "hi"}]) == "ok-k-b" and used == ["k-b"], "starts on the key that worked"
    bad_keys = {"k-a", "k-b", "k-single"}
    try:
        assistant.chat([{"role": "user", "content": "hi"}])
        raise AssertionError("expected all keys to fail")
    except RuntimeError as error:
        assert "Every configured language-model key failed" in str(error) and "k-a" not in str(error) and "429" in str(error)
finally:
    assistant.urllib.request.urlopen = real_urlopen
    os.environ.update(LLM_API_KEY="test-key", LLM_API_KEYS="")
print("Assistant passed: grounding per scope, tool routing, live farm_metric (offline farm named, scope enforced), scope detection, retry, hub answers equal the farm, fallback, independent failure, unsafe queries refused, key rotation on 429.")
