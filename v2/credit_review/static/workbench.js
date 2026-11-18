const get = id => document.getElementById(id);
const escape = value => String(value ?? "").replace(/[&<>"']/g, char =>
  ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[char]);
const titles = {
  current_ratio: "Current ratio", debt_to_ebitda: "Debt / EBITDA",
  operating_cash_coverage: "Operating cash coverage", ebitda_margin_percent: "EBITDA margin",
  ebitda: "EBITDA",
};
const label = key => titles[key] ?? String(key).replaceAll("_", " ");
const ratio = (key, value) => value === null ? "—" : Number(value).toFixed(2) + (key.endsWith("percent") ? "%" : "×");
const money = value => value === null ? "—" : "₹" +
  String(value).replace(/^(-?)(\d+)(\.\d+)?$/, (_, sign, digits, fraction = "") =>
    sign + digits.replace(/\B(?=(\d{2})+\d{3}$|\d{3}$)/g, ",") + fraction.replace(/\.?0+$/, ""));
let state;
let selectedRow;
let busy = false;

async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail :
    (data.detail?.map(item => item.msg).join(". ") || "Request failed"));
  return data;
}

const json = body => ({method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
const pdf = file => ({method: "POST", headers: {"Content-Type": "application/pdf"}, body: file});
const route = suffix => "/workspace/" + state.id + (suffix || "");

function notice(message, error = false) {
  const item = get("notice");
  item.textContent = message;
  item.toggleAttribute("data-error", error);
}

async function act(operation) {
  if (busy) return;
  busy = true;
  get("cases").disabled = true;
  notice("");
  try { await operation(); }
  catch (error) { notice(error.message, true); }
  finally {
    busy = false;
    get("cases").disabled = false;
  }
}

async function refreshCases() {
  const cases = await request("/workspace");
  get("cases").innerHTML = '<option value="">Choose a case</option>' + cases.map(item =>
    '<option value="' + escape(item.id) + '">' + escape(item.name) + '</option>').join("");
  if (state) get("cases").value = state.id;
}

function citations(ids, documentId = state.document) {
  return ids.map(id => '<button class="source" type="button" data-row="' + escape(id) + '" data-doc="' +
    escape(documentId) + '">' + escape("p" + id.split("-").at(-2) + " · line " + id.split("-").at(-1)) +
    '</button>').join(" ");
}

function showEvidence(id, documentId) {
  const row = state.analysis.rows.find(item => item.id === id);
  const page = id.split("-").at(-2);
  selectedRow = row;
  const root = route("/documents/" + documentId);
  get("preview").src = root + "/page/" + page + "?highlight=" + encodeURIComponent(id);
  get("preview").hidden = false;
  get("source-caption").textContent = row ?
    row.label + " · " + row.currency + " " + row.value + " (" + row.unit + ") · FY" + row.year :
    "Archived statement · page " + page;
  get("source-download").href = route("/source/" + documentId);
  get("source-download").hidden = false;
  const form = get("review");
  form.hidden = !row;
  if (row) {
    form.elements.account.value = row.account ?? "";
    form.elements.reason.value = row.review?.reason ?? "";
  }
  if (!window.matchMedia("(min-width: 1111px)").matches)
    get("evidence").scrollIntoView({block: "start"});
}

function render(next, preserveEvidence = false) {
  const previousRow = preserveEvidence && selectedRow?.id;
  state = next;
  const {annual, exceptions, rows} = state.analysis;
  const [prior, latest] = annual;
  get("workspace").hidden = false;
  get("case-name").textContent = state.name;
  get("case-meta").textContent = "FY" + prior.year + " / FY" + latest.year + " · Version " + state.versions.length;
  get("case-status").textContent = (state.analysis.provisional ? "Provisional · " : "Reviewed · ") +
    exceptions.length + " checks open";
  get("metrics").innerHTML = Object.entries(latest.metrics).map(([key, item]) =>
    '<div class="metric"><small>' + label(key) + '</small><strong>' + ratio(key, item.value) +
    '</strong><small>FY' + prior.year + ' ' + ratio(key, prior.metrics[key].value) + '</small><div>' +
    citations(item.refs) + '</div></div>').join("");
  get("exceptions").innerHTML = '<h3>Exceptions</h3>' + (exceptions.length ? exceptions.map(item =>
    '<div class="exception"><strong>FY' + item.year + ' · ' + escape(label(item.code)) + '</strong><div>' +
    escape(item.value ? money(item.value) : label(item.account ?? item.metric ?? "")) + '</div>' +
    citations(item.refs) + '</div>').join("") : '<p>No reconciliation exceptions.</p>');
  get("financials").innerHTML = '<h3>Financial spread</h3><div class="scroll"><table><thead><tr><th>Account</th><th>FY' +
    prior.year + '</th><th>FY' + latest.year + '</th></tr></thead><tbody>' + Object.keys(latest.accounts).map(key =>
    '<tr><th scope="row">' + label(key) + '</th>' + annual.map(period => {
      const item = period.accounts[key];
      return '<td>' + money(item.value) + '<br>' + citations(item.refs) + '</td>';
    }).join("") + '</tr>').join("") + '</tbody></table></div><details><summary>Source ledger · ' +
    rows.filter(row => !row.review).length + ' awaiting review</summary>' + rows.map(row =>
    '<div class="exception">' + escape(row.label) + ' · ' + escape(row.review ? "reviewed" : row.suggestion.method) +
    ' ' + citations([row.id]) + '</div>').join("") + '</details>';
  get("review").elements.account.innerHTML = '<option value="">Exclude source line</option>' +
    Object.keys(latest.accounts).map(key => '<option value="' + key + '">' + label(key) + '</option>').join("");
  get("scenario-result").textContent = "";
  const change = state.comparison;
  get("comparison").innerHTML = change ? '<p>' + change.reopened_reviews + ' prior reviews reopened.</p>' +
    '<a href="' + route("/source/" + change.from_document) + '">Prior statement PDF</a> · <a href="' +
    route("/source/" + change.document) + '">Current statement PDF</a>' +
    '<ul>' + change.differences.map(item => {
      const format = entry => "formula" in entry ? ratio(item.name, entry.value) : money(entry.value);
      return '<li>FY' + item.year + ' ' + label(item.name) + ' · ' + format(item.before) + ' → ' +
        format(item.after) + '<br>Prior ' + citations(item.before.refs, change.from_document) +
        ' · Current ' + citations(item.after.refs, change.document) + '</li>';
    }).join("") + '</ul>' : '<p class="muted">Upload a revised statement to compare versions.</p>';
  get("demo-replacement").hidden = state.demo !== "CR-102" || state.versions.length > 1;
  get("snapshots").innerHTML = state.snapshots.map(item =>
    '<p>' + escape(item.status) + ' · revision ' + item.revision + ' · <a href="' +
    route("/snapshots/" + item.id + ".pdf") + '">PDF</a> · <a href="' +
    route("/snapshots/" + item.id + ".json") + '">Evidence JSON</a></p>').join("");
  get("history").innerHTML = '<h3>Review history</h3>' + (state.history.length ? '<ul>' +
    [...state.history].reverse().map(event => '<li>' + escape(label(event.action)) + ' · ' +
      escape(event.decision?.reason ?? (event.reopened_reviews + " reviews reopened")) + '<br><small>' +
      escape(new Date(event.at).toLocaleString()) + '</small></li>').join("") + '</ul>' : '<p>No review actions yet.</p>');
  get("review").hidden = true;
  get("preview").hidden = true;
  get("source-download").hidden = true;
  get("source-caption").textContent = "Select a source line to inspect the statement.";
  selectedRow = undefined;
  if (previousRow && rows.some(row => row.id === previousRow)) showEvidence(previousRow, state.document);
}

document.addEventListener("click", event => {
  const button = event.target.closest("[data-row],[data-demo]");
  if (!button) return;
  if (button.dataset.row) showEvidence(button.dataset.row, button.dataset.doc);
  else act(async () => {
    render(await request("/workspace/demo/" + button.dataset.demo, {method: "POST"}));
    await refreshCases();
    get("workspace").scrollIntoView({block: "start"});
  });
});
get("cases").addEventListener("change", () => act(async () => {
  if (!get("cases").value) {get("workspace").hidden = true; state = undefined; return;}
  render(await request("/workspace/" + get("cases").value));
}));
get("upload").addEventListener("submit", event => {
  event.preventDefault();
  act(async () => {
    const form = event.target;
    render(await request("/workspace/upload?name=" + encodeURIComponent(form.elements.borrower.value), pdf(form.elements.pdf.files[0])));
    form.reset();
    await refreshCases();
  });
});
get("review").addEventListener("submit", event => {
  event.preventDefault();
  act(async () => {
    const fields = event.target.elements;
    render(await request(route("/reviews"), json({row_id: selectedRow.id, account: fields.account.value || null,
      reason: fields.reason.value, revision: state.revision})), true);
    notice("Review saved. The spread and ratios have been recalculated.");
  });
});
get("scenario").addEventListener("submit", event => {
  event.preventDefault();
  act(async () => {
    const result = await request(route("/scenario?decline=" + event.target.elements.decline.value));
    get("scenario-result").textContent = result.stressed === null ? "Cash coverage inputs are unavailable." :
      ratio("coverage", result.baseline) + " baseline → " + ratio("coverage", result.stressed) + " stressed";
  });
});
get("replacement").addEventListener("submit", event => {
  event.preventDefault();
  act(async () => {
    render(await request(route("/replacement?revision=" + state.revision), pdf(event.target.elements.pdf.files[0])));
    event.target.reset();
    notice("Replacement imported. Review the statement comparison for changes.");
  });
});
get("demo-replacement").addEventListener("click", () => act(async () => {
  render(await request(route("/replacement?demo=true&revision=" + state.revision), {method: "POST"}));
  notice("Replacement imported. Review the statement comparison for changes.");
}));
get("memo").addEventListener("submit", event => {
  event.preventDefault();
  act(async () => {
    await request(route("/snapshots"), json({revision: state.revision, notes: event.target.elements.notes.value,
      decline_percent: Number(get("scenario").elements.decline.value)}));
    render(await request(route()), true);
    notice("Memo snapshot saved. The exports preserve this evidence version.");
  });
});

act(refreshCases).finally(() => {
  get("launchers").querySelectorAll("button").forEach(button => { button.disabled = false; });
  get("upload-submit").disabled = false;
});
