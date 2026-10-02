"use strict";

const byId = id => document.getElementById(id);
const state = {csrf: null, sessionToken: null, cases: [], selected: null, busy: false, approvalMode: "human_ui", unconfirmed: new Set()};
const labels = {
  new: "Not assessed", CREATED: "Not assessed", REVIEW_READY: "Review ready", REVIEW_INCOMPLETE: "Review incomplete",
  case_open: "Not assessed", reviewing: "Assessment running", review_ready: "Review ready", review_incomplete: "Review incomplete",
  prepared: "Approved", submitting: "Submission in progress", uncertain: "Outcome uncertain",
  pending: "Awaiting verification", verified: "Refund verified", failed: "Refund failed",
  refund: "Refund recommended", decline: "Decline recommended", request_information: "More information needed"
};
const humanize = value => labels[value] || String(value || "Not assessed").replaceAll("_", " ");
const money = (minor, currency = "USD") => Number.isSafeInteger(minor) && currency === "USD"
  ? new Intl.NumberFormat("en-US", {style: "currency", currency: "USD"}).format(minor / 100) : "Amount unavailable";
const dateTime = value => {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(undefined, {timeZoneName: "short"});
};
const display = (id, visible) => { byId(id).hidden = !visible; };
const text = (id, value) => { byId(id).textContent = value == null ? "" : String(value); };

function notice(message) {
  text("alert", message);
  display("alert", Boolean(message));
  if (message) byId("alert").focus();
}

async function api(path, options = {}) {
  const headers = {...options.headers};
  if (state.sessionToken) headers["X-Refund-Desk-Session"] = state.sessionToken;
  if (options.method === "POST") {
    headers["Content-Type"] = "application/json";
    if (state.csrf) headers["X-Refund-Desk-CSRF"] = state.csrf;
  }
  let response;
  try { response = await fetch(path, {...options, headers, credentials: "omit", cache: "no-store"}); }
  catch (_) { throw new Error("Connection interrupted. Reload the saved case before taking another action; a refund may still be processing."); }
  let result;
  try { result = await response.json(); }
  catch (_) { throw new Error("The server returned an unreadable response. Check the saved case before another action."); }
  if (!response.ok) {
    if (response.status === 401) {
      state.sessionToken = null;
      try { sessionStorage.removeItem("refund-desk-session"); } catch (_) { /* Memory-only session remains possible. */ }
      display("locked", true); display("app", false); text("connection", "Private session required");
    }
    throw new Error(result.error?.message || "The request could not be completed.");
  }
  return result;
}

function setBusy(busy, message = "") {
  state.busy = busy;
  byId("app").setAttribute("aria-busy", String(busy));
  for (const control of document.querySelectorAll("#case-form input, #case-form select, #case-form textarea, #case-form button, #reload, .case-option")) control.disabled = busy;
  updateForm();
  text("activity", message);
  display("activity", busy);
  updateActions();
}

async function operation(message, work, uncertainCase = null) {
  if (state.busy) return;
  notice("");
  setBusy(true, message);
  try { await work(); }
  catch (error) {
    if (uncertainCase) state.unconfirmed.add(uncertainCase);
    notice(error.message);
  }
  finally { setBusy(false); }
}

function renderCases() {
  const list = byId("case-list");
  list.replaceChildren();
  for (const record of state.cases) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "case-option";
    button.disabled = state.busy;
    button.setAttribute("aria-current", String(record.id === state.selected?.id));
    for (const [className, value] of [["amount", money(record.amount_minor, record.currency)], ["case-id mono", record.capture_id], ["case-label", humanize(record.operation?.state || record.state)]]) {
      const line = document.createElement("span"); line.className = className; line.textContent = value; button.append(line);
    }
    button.addEventListener("click", () => operation("Loading saved case…", async () => {
      const result = await api(`/api/cases/${encodeURIComponent(record.id)}`);
      state.unconfirmed.delete(record.id);
      selectCase(result.case); byId("workspace").focus();
    }));
    list.append(button);
  }
  text("case-count", state.cases.length);
  display("no-cases", state.cases.length === 0);
}

function facts(id, rows) {
  const list = byId(id); list.replaceChildren();
  for (const [label, value] of rows) {
    const pair = document.createElement("div");
    const term = document.createElement("dt"); term.textContent = label;
    const description = document.createElement("dd"); description.textContent = value == null ? "Not recorded" : String(value);
    pair.append(term, description); list.append(pair);
  }
}

function errorText(error) {
  if (typeof error === "string") return error;
  if (error && typeof error === "object") return error.message || error.error_name || error.code || "Review could not be completed.";
  return "Review could not be completed.";
}

function sourceLabel(value) {
  if (value === "paypal_sandbox") return "PayPal sandbox response";
  if (value === "injected_test_transport" || value === "synthetic") return "Synthetic test evidence — no real PayPal verification";
  return value ? String(value).replaceAll("_", " ") : "Source not verified";
}

function reviewAnswer(review) {
  return review?.assessment?.original_model_response || review?.original_model_response || review?.answer || review || {};
}

function selectCase(record) {
  state.selected = record;
  byId("approval-check").checked = false;
  const index = state.cases.findIndex(item => item.id === record.id);
  if (index >= 0) state.cases[index] = record; else state.cases.unshift(record);
  renderCases();
  display("empty-review", false); display("case-detail", true);
  text("case-reference", "Capture " + record.capture_id);
  text("case-title", money(record.amount_minor, record.currency) + " refund request");
  const operationState = record.operation?.state;
  const caseState = operationState || record.state;
  text("case-state", humanize(caseState));
  byId("case-state").className = "status" + (["uncertain", "pending", "REVIEW_INCOMPLETE", "review_incomplete"].includes(caseState) ? " warning" : caseState === "failed" ? " error" : "");
  text("provenance", "Payment source: " + sourceLabel(record.transaction_provenance) + ". Physical condition: " + sourceLabel(record.physical_facts_provenance) + ".");
  text("case-error", record.error ? errorText(record.error) : ""); display("case-error", Boolean(record.error));
  facts("facts", [["Captured amount", money(record.amount_minor, record.currency)], ["Purchase date (UTC)", record.purchase_date],
    ["Request received (UTC)", record.request_date], ["Item condition", record.item_used === false ? "Unused · merchant confirmed" : record.item_used === true ? "Used · merchant confirmed" : "Not yet confirmed"]]);
  text("message", record.customer_message);
  const review = record.latest_review;
  const policy = record.policy || review?.policy;
  text("policy-version", policy?.version || record.policy_version || review?.policy_version || "No policy snapshot available");
  const clauses = byId("policy-clauses"); clauses.replaceChildren();
  for (const [id, quote] of Object.entries(policy?.clauses || {})) {
    const row = document.createElement("p"); row.className = "policy-clause";
    const label = document.createElement("strong"); label.textContent = id;
    row.append(label, document.createTextNode(String(quote))); clauses.append(row);
  }
  if (!clauses.childElementCount) { const row = document.createElement("p"); row.className = "field-help"; row.textContent = "The policy snapshot will appear with the saved review."; clauses.append(row); }
  display("assessment", Boolean(review));
  if (review) {
    const answer = reviewAnswer(review);
    text("recommendation", review.accepted_recommendation ? humanize(review.accepted_recommendation) : "Assessment incomplete");
    text("rationale", answer.rationale || review.rationale || "No complete AI rationale was accepted.");
    const citations = byId("citations"); citations.replaceChildren();
    for (const citation of answer.citations || review.citations || []) {
      if (!citation || typeof citation !== "object") continue;
      const block = document.createElement("blockquote");
      const quote = document.createElement("p"); quote.textContent = citation.quote || "";
      const label = document.createElement("cite"); label.textContent = "Policy " + (citation.id || "unknown") + (policy?.clauses?.[citation.id] === citation.quote ? " · exact quote" : " · not verified against this snapshot");
      block.append(quote, label); citations.append(block);
    }
    const errors = [...(review.errors || review.assessment?.errors || []), ...(review.preflight?.errors || [])];
    if (review.error) errors.push(review.error);
    const errorList = byId("review-errors"); errorList.replaceChildren();
    for (const message of [...new Set(errors.map(errorText))]) { const row = document.createElement("li"); row.textContent = message; errorList.append(row); }
    const model = review.model?.model || (typeof review.model === "string" ? review.model : null);
    const runtimeProvenance = review.runtime_provenance || review.provenance;
    const runtime = runtimeProvenance === "injected_test_generator" ? "Injected test response" : model || (runtimeProvenance === "local_ollama" ? "Local Ollama" : "Runtime source not recorded");
    text("model-evidence", `${runtime} · ${review.model_calls ?? 0} model request(s) · ${review.completed_model_responses ?? 0} completed response(s)`);
  }
  const payment = record.operation;
  display("operation", Boolean(payment));
  if (payment) {
    const approval = payment.approval || {};
    facts("operation-facts", [["Refund state", humanize(payment.state)], ["PayPal status", payment.provider_status || "Not yet read back"],
      ["Refund ID", payment.refund_id || "Not yet known"], ["Approval recorded", approval.approval_kind === "test_operator" ? "Test operator · " + (approval.approved_by || "recorded") : approval.approval_kind === "human_ui" ? "Human interface · " + (approval.approved_by || "recorded") : "Not recorded"]]);
    text("operation-error", payment.last_error ? errorText(payment.last_error) : ""); display("operation-error", Boolean(payment.last_error));
  }
  const audit = byId("audit-list"); audit.replaceChildren();
  for (const event of record.events || payment?.events || []) {
    const row = document.createElement("li");
    row.append(document.createTextNode(humanize(event.kind || event.action)));
    const time = document.createElement("time"); time.textContent = dateTime(event.occurred_at || event.created_at); if (event.occurred_at) time.dateTime = event.occurred_at;
    const detail = document.createElement("span"); detail.className = "audit-detail";
    detail.textContent = [event.from_state && humanize(event.from_state), event.to_state && humanize(event.to_state)].filter(Boolean).join(" → ");
    row.append(time, detail); audit.append(row);
  }
  if (!audit.childElementCount) { const row = document.createElement("li"); row.textContent = "No activity events returned."; audit.append(row); }
  updateActions();
}

function updateActions() {
  const record = state.selected;
  if (!record) return;
  const payment = record.operation;
  const unconfirmed = state.unconfirmed.has(record.id);
  const canReview = record.can_review === true && record.item_used !== null && typeof record.item_used === "boolean" && !payment && !unconfirmed;
  byId("review-button").disabled = state.busy || !canReview;
  text("review-button", record.latest_review?.status === "REVIEW_READY" ? "Assessment saved" : record.latest_review ? "Assess again" : "Assess request");
  text("review-guidance", payment ? "The review is frozen with the approval record." : record.item_used == null ? "Confirm the item condition before an AI review can run. No model call is available while this evidence is missing." : record.latest_review?.status === "REVIEW_READY" ? "Assessment saved. Review the recommendation and evidence below." : canReview ? "Runs the local model against this payment and the saved policy. CPU inference may take a few minutes." : record.error ? errorText(record.error) : "A complete, valid case is required before assessment.");
  const approvedAmount = money(record.amount_minor, record.currency);
  const canApprove = record.can_approve === true && (!payment || payment.state === "prepared") && !unconfirmed && Number.isSafeInteger(record.amount_minor)
    && record.amount_minor > 0 && record.currency === "USD" && /^[0-9a-f]{64}$/.test(record.latest_review?.review_hash || "");
  display("approval-controls", canApprove);
  byId("approval-check").disabled = state.busy || !canApprove;
  byId("approve-button").disabled = state.busy || !canApprove || !byId("approval-check").checked;
  text("approve-button", `Approve and refund ${approvedAmount} sandbox`);
  text("approval-label", state.approvalMode === "test_operator" ? `As a test operator, I reviewed this case and approve the ${approvedAmount} sandbox refund.` : `I reviewed the payment evidence and policy and approve this ${approvedAmount} sandbox refund.`);
  const messages = {
    uncertain: "The refund outcome is uncertain. Do not submit another refund. Check status if a refund ID is available; otherwise reconcile the existing request in PayPal.",
    submitting: "The refund submission is in progress or awaiting reconciliation. Do not submit another refund.",
    pending: "The submission has a receipt. A separate status check must verify the refund before it is called complete.",
    verified: record.transaction_provenance === "paypal_sandbox" ? "The sandbox refund was independently verified against the approved amount and capture." : "The test refund was verified using synthetic transport evidence. This is not a real PayPal refund.",
    failed: "The recorded refund request failed. This screen will not submit another refund for the same capture.",
    prepared: canApprove ? "Approval is recorded, but no refund submission has been claimed. Review and confirm below to submit this existing operation once." : "Approval is recorded. This session cannot submit the existing operation."
  };
  text("approval-guidance", unconfirmed ? "The previous action did not return a confirmed result. Reload the saved case before taking any further action. No repeat submission is available." : payment ? messages[payment.state] || "Inspect the saved operation before taking further action." : canApprove ? "This action submits one refund for the captured amount. The AI assessment alone does not authorize it." : record.latest_review?.accepted_recommendation === "decline" ? "The accepted recommendation is to decline. No refund approval is available." : "Refund approval becomes available after a complete assessment recommends a refund.");
  if (payment) {
    const refreshable = ["pending", "uncertain"].includes(payment.state) && Boolean(payment.refund_id);
    byId("refresh-button").disabled = state.busy || !refreshable;
    display("refresh-button", refreshable);
    text("refresh-guidance", payment.state === "verified" ? "Verification is saved in the activity record. No additional payment action is needed." : !payment.refund_id ? "A refund ID has not been recorded. Automatic resubmission is disabled." : "Reads the refund status separately. It does not submit another refund.");
  }
}

async function reloadCases() {
  const result = await api("/api/cases");
  state.cases = result.cases || [];
  const chosen = state.selected && state.cases.find(record => record.id === state.selected.id);
  if (chosen) { const detail = await api(`/api/cases/${encodeURIComponent(chosen.id)}`); state.unconfirmed.delete(chosen.id); selectCase(detail.case); }
  else { state.selected = null; display("empty-review", true); display("case-detail", false); renderCases(); }
}

byId("case-form").addEventListener("submit", event => {
  event.preventDefault();
  const condition = byId("item-used").value;
  if (condition === "unknown") { notice("Confirm whether the item was used before creating the review case."); return; }
  const payload = {capture_id: byId("capture-id").value.trim(), customer_message: byId("customer-message").value.trim(),
    item_used: condition === "unknown" ? null : condition === "used", request_date: byId("request-date").value};
  operation("Fetching the sandbox capture and saving the review case…", async () => {
    const result = await api("/api/cases", {method: "POST", body: JSON.stringify(payload)});
    selectCase(result.case); byId("case-form").reset(); setToday(); byId("new-case").open = false; byId("workspace").focus();
  });
});
byId("item-used").addEventListener("change", updateForm);
byId("reload").addEventListener("click", () => operation("Loading saved cases…", reloadCases));
byId("approval-check").addEventListener("change", updateActions);
byId("review-button").addEventListener("click", () => operation("Assessing this case with local AI. Keep this page open; no refund will be submitted…", async () => {
  const result = await api(`/api/cases/${encodeURIComponent(state.selected.id)}/review`, {method: "POST", body: "{}"}); selectCase(result.case);
}));
byId("approve-button").addEventListener("click", () => {
  if (!byId("approval-check").checked || !state.selected?.can_approve) return;
  const id = state.selected.id, hash = state.selected.latest_review.review_hash;
  byId("approval-check").checked = false;
  operation("Recording your approval and submitting one sandbox refund. Do not repeat this action…", async () => {
    const result = await api(`/api/cases/${encodeURIComponent(id)}/approve`, {method: "POST", body: JSON.stringify({review_hash: hash, confirmed: true})}); selectCase(result.case);
  }, id);
});
byId("refresh-button").addEventListener("click", () => operation("Reading the existing refund from PayPal sandbox. No new refund is submitted…", async () => {
  const result = await api(`/api/cases/${encodeURIComponent(state.selected.id)}/refresh`, {method: "POST", body: "{}"}); selectCase(result.case);
}));

function setToday() {
  byId("request-date").value = new Date().toISOString().slice(0, 10);
  updateForm();
}

function updateForm() {
  byId("create-case-button").disabled = state.busy || byId("item-used").value === "unknown";
}

async function start() {
  setToday();
  const fragment = new URLSearchParams(location.hash.slice(1));
  const bootstrap = fragment.get("session");
  if (location.hash) history.replaceState(null, "", location.pathname);
  try { state.sessionToken = sessionStorage.getItem("refund-desk-session"); } catch (_) { /* A private launch link also works without storage. */ }
  try {
    const session = bootstrap
      ? await api("/api/session", {method: "POST", headers: {"X-Refund-Desk-Bootstrap": bootstrap}, body: "{}"})
      : await api("/api/session");
    state.csrf = session.csrf_token; state.sessionToken = session.session_token; state.approvalMode = session.approval_mode;
    try { sessionStorage.setItem("refund-desk-session", state.sessionToken); } catch (_) { /* Keep the authenticated session in memory. */ }
    display("operator-mode", session.approval_mode === "test_operator"); display("locked", false); display("app", true);
    text("connection", "Local session connected");
    await reloadCases();
  } catch (error) { notice(error.message); }
}
window.addEventListener("hashchange", () => {
  if (new URLSearchParams(location.hash.slice(1)).get("session")) location.reload();
});
start();
