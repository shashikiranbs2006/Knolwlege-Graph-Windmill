const form = document.querySelector("form");
const queryBox = document.querySelector("#sparql");
const journey = document.querySelector("#journey");
const statusLine = document.querySelector("#journey-status");
const evidence = document.querySelector("#evidence");
const stages = [...document.querySelectorAll(".stage")];
const wires = [...document.querySelectorAll(".wire")];
const answerPanel = document.querySelector("#live-answer");
const answerTable = document.querySelector("#live-table");
const answerText = document.querySelector("#live-explanation");
const replayButton = document.querySelector("#replay");
const nextButton = document.querySelector("#next-step");
const runButton = form.querySelector('button[type="submit"]');
let run = null;

function add(tag, content, parent = evidence) {
  const node = document.createElement(tag);
  node.textContent = content;
  parent.append(node);
  return node;
}

function sourceRefs() {
  if (!run.answer) return [];
  const refs = new Set();
  for (const row of run.answer.rows) {
    for (const [file, number] of [["sourceFile", "sourceRow"], ["eventFile", "eventRow"]]) {
      if (row[file] && row[number]) refs.add(`${row[file]}, row ${row[number]}`);
    }
  }
  return [...refs];
}

function synthetic() {
  return run.reform?.relations.includes("synthetic_maintenance") ||
    run.answer?.rows.some(row => row.recordKind === "synthetic_demo");
}

function matchingRules() {
  if (!run.reform) return run.inspect.mappings;
  return run.inspect.mappings.filter(rule => {
    const source = rule.source.match(/\b(?:FROM|JOIN)\s+"?(\w+)/gi) || [];
    return source.some(fragment => run.reform.relations.includes(fragment.split(/\s+"?/).at(-1)));
  });
}

function showStage(index) {
  const visible = run.replaying ? run.shown : run.complete;
  if (index > visible) return;
  run.selected = index;
  stages.forEach((stage, i) => stage.classList.toggle("current", i === index));
  evidence.replaceChildren();
  const titles = ["Your submitted query", "Graph terms in your query", "Matching R2RML rules", "Ontop's reformulation", "PostgreSQL relations in Ontop's SQL", "Live answer and source rows"];
  add("h3", titles[index]);

  if (index === 0) {
    add("p", "This is the query you typed. Highlighted terms were found in the V1 ontology or runtime mapping.");
    const pre = add("pre", "");
    const tokens = run.inspect?.terms.map(term => term.token).sort((a, b) => b.length - a.length) || [];
    const pattern = tokens.length && new RegExp(tokens.map(token => token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");
    if (!pattern) pre.textContent = run.query;
    else {
      let start = 0;
      for (const match of run.query.matchAll(pattern)) {
        pre.append(document.createTextNode(run.query.slice(start, match.index)));
        add("mark", match[0], pre);
        start = match.index + match[0].length;
      }
      pre.append(document.createTextNode(run.query.slice(start)));
    }
  } else if (index === 1) {
    const terms = run.inspect.terms;
    add("p", `${terms.length} graph term(s) matched definitions or mapping targets. These are terms in your query, not database columns.`);
    const list = add("ul", "");
    for (const term of terms) {
      const item = add("li", `${term.token} → ${term.iri}`, list);
      add("small", ` ${term.definition}`, item);
    }
    if (!terms.length) add("p", "No directly indexed V1 term was found. Ontop's SQL remains the authoritative source trace.");
  } else if (index === 2) {
    add("p", run.reform ? "These rules match query terms and refer to relations in Ontop's SQL. Several rules can share a relation, so this is a matching-rule list, not an Ontop rule-execution log." : "These rules match query terms. Ontop's SQL will narrow the list to relevant database relations.");
    for (const rule of matchingRules()) {
      const detail = add("details", "");
      add("summary", `${rule.id} · ${rule.terms.join(", ")}`, detail);
      add("p", `Mapping SQL source: ${rule.source || "See rule snippet"}`, detail);
      add("pre", rule.snippet, detail);
    }
    if (!matchingRules().length) add("p", run.reform ? "No direct R2RML term match for the confirmed SQL relations. Inspect Ontop's generated SQL in the next stage." : "No direct R2RML term match. Ontop will still try to reformulate your query.");
  } else if (index === 3) {
    add("p", "Returned by this Ontop instance for the submitted query. The output includes Ontop's plan and its generated SQL.");
    add("pre", run.reform.reformulation);
  } else if (index === 4) {
    const relations = run.reform.relations;
    add("p", "Only relations named in Ontop's generated SQL are listed here.");
    if (relations.length) for (const relation of relations) add("span", relation, evidence).className = "badge";
    else add("p", "No PostgreSQL relation was found in the reformulation. Inspect Ontop's raw output in the preceding stage.");
    for (const [name, definition] of Object.entries(run.reform.views)) {
      const detail = add("details", "");
      add("summary", `${name} is a view; show its V1 schema definition`, detail);
      add("pre", definition, detail);
    }
    if (synthetic()) add("p", "synthetic_demo: maintenance cases are fabricated examples, not operator-confirmed repairs.").className = "warning";
  } else {
    add("p", `Ontop returned ${run.answer.rows.length} row(s) from PostgreSQL for this query.`);
    const refs = sourceRefs();
    if (refs.length) {
      add("p", "Source references returned by your query:");
      const list = add("ul", "");
      for (const ref of refs.slice(0, 8)) add("li", ref, list);
      if (refs.length > 8) add("li", `${refs.length - 8} more references in the answer table.`, list);
    } else add("p", "This query did not select source file and row fields, so no exact CSV row is claimed.");
    if (synthetic()) add("p", "synthetic_demo: any maintenance case in this path is fabricated for the demo.").className = "warning";
  }
}

function paint() {
  const visible = run.replaying ? run.shown : run.complete;
  stages.forEach((stage, i) => {
    stage.classList.toggle("done", i <= visible);
    stage.classList.toggle("waiting", i > visible);
    stage.querySelector("span").textContent = i <= visible ? "Evidence ready" : "Waiting";
  });
  wires.forEach((wire, i) => {
    wire.classList.toggle("done", i < visible);
    wire.classList.toggle("flowing", i === visible - 1 && (run.complete < 5 || run.replaying));
  });
  nextButton.disabled = !run.replaying || run.shown >= run.complete;
  if (run.selected > visible) run.selected = visible;
  showStage(run.selected);
}

function advance(index, message) {
  run.complete = index;
  run.selected = index;
  statusLine.textContent = message;
  paint();
}

async function phase(path, query) {
  const response = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/x-www-form-urlencoded"},
    body: new URLSearchParams({sparql: query}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

function renderAnswer() {
  const {columns, rows} = run.answer;
  answerPanel.hidden = false;
  answerPanel.querySelectorAll(".warning").forEach(node => node.remove());
  document.querySelector("#live-answer-title").textContent = `Answer · ${rows.length} row(s)`;
  answerTable.replaceChildren();
  if (columns.length) {
    const table = add("table", "", answerTable);
    const head = add("thead", "", table);
    const heading = add("tr", "", head);
    for (const column of columns) add("th", column, heading).scope = "col";
    const body = add("tbody", "", table);
    for (const row of rows) {
      const tr = add("tr", "", body);
      for (const column of columns) add("td", row[column] || "", tr);
    }
  }
  answerText.textContent = `Live Ontop answer. ${run.reform.relations.length ? `PostgreSQL relations in its SQL: ${run.reform.relations.join(", ")}.` : "Inspect the Ontop reformulation for its source."} ${sourceRefs().length ? "Source file and row references are in the answer and journey above." : "Exact source rows were not selected by this query."}`;
  if (synthetic()) add("p", "synthetic_demo: linked maintenance data is fabricated for this demo.", answerPanel).className = "warning";
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  const query = queryBox.value.trim();
  if (!query) return;
  run = {query, inspect: null, reform: null, answer: null, complete: 0, shown: 0, selected: 0, replaying: false};
  journey.hidden = false;
  answerPanel.hidden = true;
  statusLine.className = "";
  document.querySelector("#legacy-result")?.setAttribute("hidden", "");
  replayButton.disabled = true;
  nextButton.disabled = true;
  runButton.disabled = true;
  advance(0, "Query submitted. Checking graph terms against the runtime mapping…");
  try {
    run.inspect = await phase("/inspect", query);
    advance(1, "Graph terms identified from your query.");
    advance(2, "Matching R2RML rules found. Asking Ontop for its SQL…");
    run.reform = await phase("/reformulate", query);
    advance(3, "Ontop returned its reformulation and generated SQL.");
    advance(4, "PostgreSQL relations identified from Ontop's SQL. Running the query…");
    run.answer = await phase("/answer", query);
    advance(5, `Answer ready: ${run.answer.rows.length} row(s) returned.`);
    renderAnswer();
    replayButton.disabled = false;
  } catch (error) {
    statusLine.textContent = `Stopped: ${error.message}`;
    statusLine.className = "error";
    add("p", error.message).className = "error";
  } finally {
    runButton.disabled = false;
  }
});

stages.forEach((stage, index) => stage.addEventListener("click", () => run && showStage(index)));
replayButton.addEventListener("click", () => {
  if (!run?.answer) return;
  run.replaying = true;
  run.shown = 0;
  run.selected = 0;
  statusLine.className = "";
  statusLine.textContent = "Replay of the completed live query. Press Next step to inspect each piece of evidence.";
  paint();
});
nextButton.addEventListener("click", () => {
  if (!run?.replaying || run.shown >= run.complete) return;
  run.shown++;
  run.selected = run.shown;
  paint();
});
