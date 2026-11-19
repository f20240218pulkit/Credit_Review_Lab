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
let csrfToken;
let casesList = [];
let organizations = [];
const signedIn = !get("team-tools").hidden;
const workflowForm = get("workflow-form");
const statusLabel = status => String(status).replaceAll("_", " ");
const incomingInvite = new URLSearchParams(location.hash.slice(1)).get("invite");
if (incomingInvite && /^[A-Za-z0-9_-]{43}$/.test(incomingInvite)) {
  sessionStorage.setItem("credit-review-invite", incomingInvite);
  history.replaceState(null, "", location.pathname + location.search);
}
const invitation = sessionStorage.getItem("credit-review-invite");
if (invitation) {
  get("invite-panel").hidden = false;
  get("invite-message").textContent = signedIn ? "Accept with the verified email named in your invitation." :
    "Sign in using the verified email named in your invitation.";
  get("invite-sign-in").hidden = signedIn;
  get("accept-invite").hidden = !signedIn;
}

async function request(path, options) {
  if (options?.method === "POST") {
    if (!csrfToken) await request("/workspace");
    const headers = new Headers(options.headers);
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
    options = {...options, headers};
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail :
    (data.detail?.map(item => item.msg).join(". ") || "Request failed"));
  csrfToken = response.headers.get("X-CSRF-Token") ?? csrfToken;
  return data;
}

const json = body => ({method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
const statementType = file => /\.csv$/i.test(file.name) ? "text/csv" : "application/pdf";
const statementUpload = file => ({method: "POST", headers: {
  "Content-Type": statementType(file),
}, body: file});
const route = suffix => "/workspace/" + state.id + (suffix || "");

function notice(message, error = false) {
  const item = get("notice");
  item.textContent = message;
  item.toggleAttribute("data-error", error);
}

async function act(operation, button, pendingLabel) {
  if (busy) return;
  busy = true;
  const cases = get("cases");
  const buttonLabel = button?.textContent;
  cases.disabled = true;
  if (button) {
    button.setAttribute("aria-busy", "true");
    button.setAttribute("aria-disabled", "true");
    button.textContent = pendingLabel;
  }
  notice("");
  try { await operation(); }
  catch (error) { notice(error.message, true); }
  finally {
    if (button) {
      button.textContent = buttonLabel;
      button.removeAttribute("aria-busy");
      button.removeAttribute("aria-disabled");
    }
    cases.value = state?.id ?? "";
    cases.disabled = cases.options.length < 2;
    busy = false;
  }
}

function renderInbox() {
  const term = get("case-search").value.toLowerCase();
  const showArchived = get("show-archived").checked;
  const filtered = casesList.filter(item => (showArchived || !item.archived) &&
    (item.name + " " + statusLabel(item.status)).toLowerCase().includes(term));
  get("inbox-results").innerHTML = filtered.map(item =>
    '<div class="case-inbox-row"><span>' + escape(item.name) + ' · ' +
    escape(item.archived ? "archived" : statusLabel(item.status)) + '</span>' +
    '<button type="button" class="button-secondary" data-case="' + escape(item.id) + '">Open</button></div>'
  ).join("") || '<p class="muted">No cases match.</p>';
}

async function refreshCases() {
  casesList = await request("/workspace");
  const active = casesList.filter(item => !item.archived || item.id === state?.id);
  const picker = get("cases");
  picker.replaceChildren(
    new Option(active.length ? "Select a borrower case" : "No saved cases yet", "", true, true),
    ...active.map(item => new Option(item.name, item.id)),
  );
  picker.options[0].disabled = true;
  get("case-picker-help").textContent = active.length ? "Resume a saved borrower review." :
    (get("upload").hidden ? "Open a demo to get started." : "Open a demo or upload a statement.");
  if (signedIn) {
    get("case-inbox").hidden = false;
    renderInbox();
  }
}

async function refreshOrganizations() {
  organizations = await request("/workspace/organizations");
  const managed = organizations.filter(team => team.role === "admin");
  const writable = organizations.filter(team => team.role !== "reviewer");
  get("upload-organization").replaceChildren(new Option("Personal workspace", ""),
    ...writable.map(team => new Option(team.name, team.id)));
  get("upload-team-choice").hidden = writable.length === 0;
  get("invite-team").replaceChildren(...managed.map(team => new Option(team.name, team.id)));
  get("invite-member").hidden = managed.length === 0;
  if (managed.length) await refreshTeamMembers();
}

async function openCase(id) {
  if (!casesList.some(item => item.id === id)) await refreshCases();
  render(await request("/workspace/" + encodeURIComponent(id)));
  focusCase();
}

function editableCase(forReplacement = false) {
  const status = state.workflow?.status;
  return !state.archived && state.access?.role !== "reviewer" && status !== "in_review" &&
    (status !== "signed_off" || forReplacement);
}

function renderWorkflow() {
  const teamCase = Boolean(state.access);
  const status = state.workflow?.status || "draft";
  const submit = state.access?.role !== "reviewer" && ["draft", "changes_requested"].includes(status);
  const decide = state.access?.assigned_to_me && status === "in_review";
  get("review-workflow").hidden = !teamCase;
  get("workflow-status").textContent = "Status: " + statusLabel(status);
  const recall = status === "in_review" && (state.access?.role === "admin" || state.access?.submitted_by_me);
  workflowForm.hidden = !teamCase || state.archived || !(submit || decide || recall);
  get("reviewer-choice").hidden = !submit;
  get("assigned-reviewer").required = submit;
  workflowForm.querySelector('[value="submit"]').hidden = !submit;
  workflowForm.querySelector('[value="submit"]').disabled = true;
  workflowForm.querySelector('[value="request_changes"]').hidden = !decide;
  workflowForm.querySelector('[value="sign_off"]').hidden = !decide;
  workflowForm.querySelector('[value="recall"]').hidden = !recall;
  const archive = get("archive-case");
  archive.hidden = !signedIn || state.access?.role === "reviewer" || status === "in_review";
  archive.textContent = state.archived ? "Restore case" : "Archive case";
  if (teamCase && submit) {
    const current = state;
    request("/workspace/organizations/" + state.access.organization + "/reviewers").then(reviewers => {
      if (state !== current) return;
      get("assigned-reviewer").replaceChildren(...reviewers.map(person => new Option(person.label, person.id)));
      workflowForm.querySelector('[value="submit"]').disabled = reviewers.length === 0;
      if (!reviewers.length) get("workflow-status").append(" · Invite a reviewer to submit this case.");
    }).catch(error => notice(error.message, true));
  }
}

function citations(ids, documentId = state.document) {
  return ids.map(id => '<button class="source" type="button" aria-controls="evidence" aria-pressed="false" data-row="' + escape(id) + '" data-doc="' +
    escape(documentId) + '">' + escape("p" + id.split("-").at(-2) + " · line " + id.split("-").at(-1)) +
    '</button>').join(" ");
}

function showEvidence(id, documentId, navigate = true) {
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
  form.hidden = !row || !editableCase();
  if (row) {
    form.elements.account.value = row.account ?? "";
    form.elements.reason.value = row.review?.reason ?? "";
  }
  document.querySelectorAll(".source").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.row === id && button.dataset.doc === documentId));
  });
  if (navigate) {
    get("evidence-title").focus({preventScroll: true});
    get("evidence-title").scrollIntoView({block: "nearest"});
  }
}

function focusCase() {
  get("case-name").focus({preventScroll: true});
  get("workspace").scrollIntoView({block: "start"});
}

function render(next, preserveEvidence = false) {
  const previousRow = preserveEvidence && selectedRow?.id;
  state = next;
  const url = new URL(location.href);
  url.searchParams.set("case", state.id);
  history.replaceState(null, "", url);
  renderWorkflow();
  const {annual, exceptions, rows} = state.analysis;
  const [prior, latest] = annual;
  get("workspace").hidden = false;
  get("case-name").textContent = state.name;
  get("case-meta").textContent = "FY" + prior.year + " / FY" + latest.year + " · Version " + state.versions.length;
  get("case-status").textContent = (state.archived ? "Archived · " : state.analysis.provisional ? "Provisional · " : "Reviewed · ") +
    exceptions.length + " checks open";
  get("case-status").toggleAttribute("data-provisional", state.analysis.provisional);
  get("metrics").innerHTML = Object.entries(latest.metrics).map(([key, item]) =>
    '<div class="metric"><span class="metric-name">' + label(key) + '</span><strong>' + ratio(key, item.value) +
    '</strong><small>FY' + prior.year + ' ' + ratio(key, prior.metrics[key].value) + '</small><div class="metric-sources">' +
    citations(item.refs) + '</div></div>').join("");
  get("exceptions").toggleAttribute("data-has-exceptions", exceptions.length > 0);
  get("exceptions").innerHTML = '<h3>Exceptions</h3>' + (exceptions.length ? '<div class="exception-list">' + exceptions.map(item =>
    '<div class="exception"><strong>FY' + item.year + ' · ' + escape(label(item.code)) + '</strong><div>' +
    escape(item.value ? money(item.value) : label(item.account ?? item.metric ?? "")) + '</div>' +
    citations(item.refs) + '</div>').join("") + '</div>' : '<p class="muted">No reconciliation exceptions.</p>');
  get("financials").innerHTML = '<h3>Financial spread</h3><div class="scroll" role="region" aria-label="Financial spread table" tabindex="0"><table><thead><tr><th>Account</th><th>FY' +
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
  get("demo-replacement").hidden = state.demo !== "CR-102" || state.versions.length > 1 || !editableCase(true);
  get("replacement").querySelector("button").disabled = !editableCase(true);
  get("memo").querySelector("button").disabled = !editableCase();
  const csv = state.versions.find(item => item.document === state.document)?.source_type === "csv";
  get("original-csv").hidden = !csv;
  if (csv) get("original-csv").href = route("/original/" + state.document);
  get("snapshots").innerHTML = state.snapshots.map(item =>
    '<p>' + escape(item.status) + ' · revision ' + item.revision + ' · <a href="' +
    route("/snapshots/" + item.id + ".pdf") + '">PDF</a> · <a href="' +
    route("/snapshots/" + item.id + ".json") + '">Evidence JSON</a></p>').join("");
  get("history").innerHTML = '<h3>Review history</h3>' + (state.history.length ? '<ul>' +
    [...state.history].reverse().map(event => '<li>' + escape(label(event.action)) + ' · ' +
      escape(event.decision?.reason ?? event.reason ?? (event.reopened_reviews === undefined ? "" : event.reopened_reviews + " reviews reopened")) + '<br><small>' +
      escape(new Date(event.at).toLocaleString()) + '</small></li>').join("") + '</ul>' : '<p>No review actions yet.</p>');
  get("review").hidden = true;
  get("preview").hidden = true;
  get("source-download").hidden = true;
  get("source-caption").textContent = "Select a page or line reference to see it here.";
  selectedRow = undefined;
  if (previousRow && rows.some(row => row.id === previousRow)) showEvidence(previousRow, state.document, false);
}

document.addEventListener("click", event => {
  const button = event.target.closest("[data-row],[data-demo],[data-case]");
  if (!button) return;
  if (button.dataset.row) showEvidence(button.dataset.row, button.dataset.doc);
  else if (button.dataset.case) act(() => openCase(button.dataset.case), button, "Opening…");
  else act(async () => {
    render(await request("/workspace/demo/" + button.dataset.demo, {method: "POST"}));
    await refreshCases();
    focusCase();
  }, button, "Opening demo…");
});
get("cases").addEventListener("change", event => {
  const caseId = event.target.value;
  if (caseId) act(() => openCase(caseId));
});
let watchedJob;
async function watchIntake(id) {
  watchedJob = id;
  get("intake-status").hidden = false;
  get("intake-open").hidden = true;
  get("intake-resume").hidden = true;
  get("intake-progress").textContent = "Checking the statement…";
  try {
    for (let attempt = 0; attempt < 30 && watchedJob === id; attempt++) {
      let job = await request("/workspace/intake/" + id);
      if (job.status === "awaiting_upload" && sessionStorage.getItem("credit-review-intake") === id) {
        job = await request("/workspace/intake/" + id + "/finalize", {method: "POST"});
      }
      if (watchedJob !== id) return;
      if (job.status === "complete") {
        get("intake-progress").textContent = "Your statement is ready to review.";
        get("intake-open").dataset.case = job.case_id;
        get("intake-open").hidden = false;
        return;
      }
      if (["awaiting_upload", "failed", "expired"].includes(job.status)) {
        get("intake-progress").textContent = job.error || "This upload was interrupted or expired. Upload the file again.";
        sessionStorage.removeItem("credit-review-intake");
        return;
      }
      get("intake-progress").textContent = statusLabel(job.status) + " · You can continue using the workspace.";
      await new Promise(resolve => setTimeout(resolve, 2000));
    }
    if (watchedJob !== id) return;
    get("intake-progress").textContent = "Your statement is still processing.";
  } catch (error) {
    if (watchedJob !== id) return;
    get("intake-progress").textContent = error.message;
  }
  get("intake-resume").hidden = false;
}

async function privateUpload(file, params, replacement = {}) {
  const bytes = await file.arrayBuffer();
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  const checksum = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, "0")).join("");
  const intent = await request("/workspace/intake", json({
    name: params.get("name"), organization: params.get("organization"), size: file.size,
    media_type: statementType(file), checksum, ...replacement,
  }));
  const body = new FormData();
  for (const [key, value] of Object.entries(intent.target.fields)) body.append(key, value);
  body.append("file", file);
  const uploaded = await fetch(intent.target.url, {method: "POST", body});
  if (!uploaded.ok) throw new Error("Private storage did not accept the file. Try again.");
  sessionStorage.setItem("credit-review-intake", intent.id);
  watchIntake(intent.id);
}

get("intake-resume").addEventListener("click", () => watchIntake(watchedJob));

get("upload").addEventListener("submit", event => {
  event.preventDefault();
  act(async () => {
    const form = event.target;
    const params = new URLSearchParams({name: form.elements.borrower.value});
    if (form.elements.organization.value) params.set("organization", form.elements.organization.value);
    const file = form.elements.pdf.files[0];
    if (document.body.hasAttribute("data-private-intake") && params.has("organization")) {
      await privateUpload(file, params);
      form.reset();
      return;
    }
    render(await request("/workspace/upload?" + params, statementUpload(file)));
    form.reset();
    await refreshCases();
    focusCase();
  }, event.submitter, "Uploading statements…");
});
get("review").addEventListener("submit", event => {
  event.preventDefault();
  act(async () => {
    const fields = event.target.elements;
    render(await request(route("/reviews"), json({row_id: selectedRow.id, account: fields.account.value || null,
      reason: fields.reason.value, revision: state.revision})), true);
    notice("Review saved. The spread and ratios have been recalculated.");
  }, event.submitter, "Saving review…");
});
get("scenario").addEventListener("submit", event => {
  event.preventDefault();
  act(async () => {
    const result = await request(route("/scenario?decline=" + event.target.elements.decline.value));
    get("scenario-result").textContent = result.stressed === null ? "Cash coverage inputs are unavailable." :
      ratio("coverage", result.baseline) + " baseline → " + ratio("coverage", result.stressed) + " stressed";
  }, event.submitter, "Calculating…");
});
get("replacement").addEventListener("submit", event => {
  event.preventDefault();
  act(async () => {
    const file = event.target.elements.pdf.files[0];
    if (document.body.hasAttribute("data-private-intake") && state.access) {
      await privateUpload(file, new URLSearchParams({name: state.name, organization: state.access.organization}),
        {case_id: state.id, revision: state.revision});
      event.target.reset();
      return;
    }
    render(await request(route("/replacement?revision=" + state.revision), statementUpload(file)));
    event.target.reset();
    notice("Replacement imported. Review the statement comparison for changes.");
  }, event.submitter, "Uploading revision…");
});
get("demo-replacement").addEventListener("click", event => act(async () => {
  render(await request(route("/replacement?demo=true&revision=" + state.revision), {method: "POST"}));
  notice("Replacement imported. Review the statement comparison for changes.");
}, event.currentTarget, "Loading revision…"));
get("memo").addEventListener("submit", event => {
  event.preventDefault();
  act(async () => {
    await request(route("/snapshots"), json({revision: state.revision, notes: event.target.elements.notes.value,
      decline_percent: Number(get("scenario").elements.decline.value)}));
    render(await request(route()), true);
    notice("Memo snapshot saved. The exports preserve this evidence version.");
  }, event.submitter, "Saving snapshot…");
});

get("case-search").addEventListener("input", renderInbox);
get("show-archived").addEventListener("change", renderInbox);
async function refreshTeamMembers() {
  const id = get("invite-team").value;
  const base = "/workspace/organizations/" + id;
  const [members, intake] = await Promise.all([request(base + "/members"), request(base + "/intake-summary")]);
  const list = get("team-members");
  list.hidden = false;
  list.innerHTML = members.map(member => '<div class="case-inbox-row"><span>' + escape(member.name) + ' · ' +
    escape(member.role) + '</span>' + (member.role === "admin" ? "" :
      '<button type="button" class="button-secondary" data-revoke="' + escape(member.id) + '">Remove access</button>') +
    '</div>').join("");
  get("team-intake").textContent = Object.keys(intake.counts).length ? "Intake in the last 7 days: " +
    Object.entries(intake.counts).map(([status, count]) => statusLabel(status) + " " + count).join(" · ") : "";
}
get("invite-team").addEventListener("change", () => act(refreshTeamMembers));
get("team-members").addEventListener("click", event => {
  const button = event.target.closest("[data-revoke]");
  if (!button || !window.confirm("Remove this member’s access to the team and its cases?")) return;
  act(async () => {
    await request("/workspace/organizations/" + get("invite-team").value + "/members/" +
      encodeURIComponent(button.dataset.revoke) + "/revoke", {method: "POST"});
    await refreshTeamMembers();
    notice("Team access removed.");
  }, button, "Removing…");
});
get("create-team").addEventListener("submit", event => {
  event.preventDefault();
  act(async () => {
    await request("/workspace/organizations", json({name: event.target.elements.name.value}));
    event.target.reset();
    await refreshOrganizations();
    notice("Team created.");
  }, event.submitter, "Creating…");
});
get("invite-member").addEventListener("submit", event => {
  event.preventDefault();
  act(async () => {
    const fields = event.target.elements;
    const result = await request("/workspace/organizations/" + get("invite-team").value + "/invitations",
      json({name: fields.member_name.value, email: fields.email.value, role: fields.role.value}));
    get("invite-link").value = new URL(result.path, location.origin).href;
    get("invite-result").hidden = false;
    get("invite-link").focus();
    get("invite-link").select();
  }, event.submitter, "Creating invitation…");
});
get("accept-invite").addEventListener("click", event => act(async () => {
  await request("/workspace/invitations/accept", json({token: invitation}));
  sessionStorage.removeItem("credit-review-invite");
  get("invite-panel").hidden = true;
  await refreshOrganizations();
  await refreshCases();
  notice("You joined the team.");
}, event.currentTarget, "Joining…"));
workflowForm.addEventListener("submit", event => {
  event.preventDefault();
  const button = event.submitter;
  act(async () => {
    const fields = workflowForm.elements;
    render(await request(route("/workflow"), json({
      action: button.value, reviewer: fields.reviewer.value || null, notes: fields.notes.value,
      decline_percent: Number(get("scenario").elements.decline.value), revision: state.revision,
    })));
    fields.notes.value = "";
    await refreshCases();
    notice("Team review updated.");
  }, button, "Saving…");
});
get("archive-case").addEventListener("click", () => act(async () => {
  render(await request(route("/archive"), json({revision: state.revision, archived: !state.archived})));
  await refreshCases();
  notice(state.archived ? "Case archived." : "Case restored.");
}));
act(async () => {
  await refreshCases();
  if (signedIn) {
    await refreshOrganizations();
    const pending = sessionStorage.getItem("credit-review-intake");
    if (pending) watchIntake(pending);
  }
  const caseId = new URLSearchParams(location.search).get("case");
  if (caseId && casesList.some(item => item.id === caseId)) await openCase(caseId);
}).finally(() => {
  get("launchers").querySelectorAll("button").forEach(button => { button.disabled = false; });
  get("upload-submit").disabled = false;
});
