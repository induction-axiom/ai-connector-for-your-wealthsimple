import {initializeApp} from "https://www.gstatic.com/firebasejs/12.3.0/firebase-app.js";
import {getAuth, GoogleAuthProvider, signInWithPopup, browserSessionPersistence,
  setPersistence, onAuthStateChanged, signOut}
  from "https://www.gstatic.com/firebasejs/12.3.0/firebase-auth.js";

const el = id => document.getElementById(id);
const VIEWS = ["overview", "developer"];
let auth, user, status, connectMode = null, showAmounts = false, busy = false;

// ---------- Small helpers ----------

class OwnerSessionExpired extends Error {}

async function api(path, body = {}) {
  const response = await fetch(path, {method: "POST", credentials: "same-origin",
    headers: {"Authorization": "Bearer " + await user.getIdToken(),
      "Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await response.json().catch(() => ({}));
  if (response.status === 403 && data.error === "owner_login_required") throw new OwnerSessionExpired();
  if (!response.ok && !data.result) throw new Error(data.error || "request_failed");
  return data;
}

function ago(value) {
  if (!value) return null;
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const seconds = (date.getTime() - Date.now()) / 1000;
  const [size, unit] = [[86400, "day"], [3600, "hour"], [60, "minute"], [1, "second"]]
    .find(([size]) => Math.abs(seconds) >= size) || [1, "second"];
  return new Intl.RelativeTimeFormat(undefined, {numeric: "auto"}).format(Math.round(seconds / size), unit);
}

function monthDay(value) {
  const date = typeof value === "number" ? new Date(value * 1000) : null;
  if (!date || Number.isNaN(date.getTime())) return null;
  const year = date.getFullYear() === new Date().getFullYear() ? undefined : "numeric";
  return new Intl.DateTimeFormat(undefined, {month: "short", day: "numeric", year}).format(date);
}

function monthYear(value) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime())
    ? new Intl.DateTimeFormat(undefined, {month: "short", year: "numeric"}).format(date) : null;
}

// Unknown stays unknown: never show a missing count as 0.
const count = value => Number.isInteger(value) ? value.toLocaleString() : "—";

const HIDDEN = "••••";
const amount = value => value && Number.isFinite(Number(value.amount)) ? Number(value.amount) : null;

function currency(value, code) {
  if (!showAmounts) return HIDDEN;
  if (value === null) return "—";
  return new Intl.NumberFormat(undefined, {style: "currency", currency: code || "CAD",
    maximumFractionDigits: Math.abs(value) >= 1000 ? 0 : 2}).format(value);
}

const percent = (value, signed = false) => value === null || !Number.isFinite(value) ? "—"
  : new Intl.NumberFormat(undefined, {style: "percent", maximumFractionDigits: 1,
      signDisplay: signed ? "exceptZero" : "auto"}).format(value);

function shortDate(value) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime())
    ? new Intl.DateTimeFormat(undefined, {month: "short", day: "numeric"}).format(date) : "";
}

function listItem(title, detail, value, tone = "") {
  const row = document.createElement("li");
  const text = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = title;
  const small = document.createElement("span");
  small.textContent = detail;
  text.append(strong, small);
  const figure = document.createElement("span");
  figure.className = "value " + tone;
  figure.textContent = value;
  row.append(text, figure);
  return row;
}

function emptyItem(text) {
  const row = document.createElement("li");
  row.className = "muted";
  row.textContent = text;
  return row;
}

function say(id, text, tone = "neutral") {
  el(id).textContent = text;
  el(id).dataset.tone = tone;
}

function chip(id, text, tone = "") {
  el(id).textContent = text;
  el(id).className = "chip " + tone;
}

// ---------- Plain-language copy for internal result codes ----------

function explainSyncError(code) {
  if (!code) return null;
  if (code === "rate_limited_stop") return "Wealthsimple asked the connector to slow down. Try again in a few minutes.";
  if (code === "sync_already_running") return "Another sync was running at the same time.";
  if (code === "sync_deadline_reached") return "The sync took too long and stopped.";
  if (/shape_changed|graphql|_http_failed|not_json|response_invalid|data_missing/.test(code))
    return "Wealthsimple answered in an unexpected format. The connector may need an update.";
  return "The last sync didn't finish.";
}

const REFRESH_COPY = {
  refresh_succeeded: ["Your data is up to date.", "success"],
  refresh_partial: ["Updated. Some activity couldn't be read and was skipped.", "warning"],
  refresh_continues: ["Your history is still loading. It picks up again on the next sync.", "neutral"],
  refresh_cooldown: ["Your portfolio was synced a few minutes ago.", "neutral"],
  refresh_reused: ["Your activity was synced a few minutes ago.", "neutral"],
  saved_page: ["Your data is up to date.", "success"],
  sync_already_running: ["A sync is already running. Try again in a few minutes.", "warning"],
  refresh_failed: ["The sync didn't finish. Your saved data is unchanged.", "danger"],
  refresh_result_unknown: ["The sync may not have finished. Your saved data is unchanged.", "danger"],
  reconnect_required: ["Wealthsimple signed the connector out. Reconnect to resume updates.", "danger"],
};

// Where Wealthsimple sent the code, from its challenge; "app" is an authenticator app.
function otpHint({method, hint}) {
  if (method === "app") return "Enter the 6-digit code from your authenticator app.";
  if (method === "sms" || method === "recovery_sms")
    return hint ? `Enter the code Wealthsimple texted to the number ending in ${hint}.`
      : "Enter the code Wealthsimple texted to your phone.";
  if (method === "email") return "Enter the code Wealthsimple emailed you.";
  return "Enter the code from your authenticator app, or the one Wealthsimple texted or emailed you.";
}

const CONNECT_COPY = {
  login_rejected: "Wealthsimple didn't accept that email and password.",
  rate_limited_stop: "Wealthsimple is limiting sign-in attempts. Wait a few minutes and try again.",
  sync_already_running: "A sync is running. Try again when it finishes.",
  reconnect_input_invalid: "Check the email, password and code.",
};

// ---------- Views and routing ----------

function currentView() {
  const name = location.hash.slice(1);
  return VIEWS.includes(name) ? name : "overview";
}

function showView() {
  const name = currentView();
  for (const view of VIEWS) el(view + "-view").classList.toggle("hidden", view !== name);
  for (const tab of el("tabs").querySelectorAll("a"))
    tab.setAttribute("aria-current", tab.dataset.view === name ? "page" : "false");
  if (name === "developer" && user) preview(previewTarget);
  if (name === "overview" && status) fitRecent();
  window.scrollTo({top: 0});
}

function showSignedIn(signedIn) {
  el("signin-view").classList.toggle("hidden", signedIn);
  el("tabs").classList.toggle("hidden", !signedIn);
  el("account").classList.toggle("hidden", !signedIn);
  if (signedIn) showView();
  else for (const view of VIEWS) el(view + "-view").classList.add("hidden");
}

// ---------- Overview ----------

// "Gemini", "Gemini and Claude", "3 apps"
function appNames(apps) {
  const names = [...new Set(apps.map(appName))];
  return names.length > 2 ? names.length + " apps" : names.join(" and ");
}

// The page answers one question first: can my AI read my latest data?
function overallState(portfolio, activities, apps) {
  if (portfolio.connection_state === "not_connected") return {
    tone: "neutral", label: "Setup", title: "Connect Wealthsimple.", action: "connect",
    summary: "Sign in once. The connector then keeps a private, read-only copy of your portfolio for your AI apps.",
  };
  if (portfolio.reconnect_required) return {
    tone: "danger", label: "Action required", title: "Reconnect Wealthsimple.", action: "reconnect",
    summary: "Your saved data is still available to your AI apps. Reconnect to resume updates.",
  };
  const hasPortfolio = portfolio.snapshot_available === true;
  const hasActivity = activities.available === true;
  if (!hasPortfolio && !hasActivity) return {
    tone: "neutral", label: "Ready", title: "Run your first sync.", action: "sync",
    summary: "Wealthsimple is connected. Sync to save your portfolio and activity.",
  };
  const problem = explainSyncError(portfolio.last_sync_error || activities.error);
  if (problem) return {
    tone: "warning", label: "Needs attention", title: "The last sync didn't finish.", action: "sync",
    summary: problem + " Your saved data is still available.",
  };
  const updated = ago(portfolio.fetched_at);
  // Age alone isn't a problem: your AI asks for fresh data whenever it reads.
  if (!hasPortfolio || !hasActivity) return {
    tone: "warning", label: "Incomplete", title: "Part of your data is missing.", action: "sync",
    summary: "Sync to save both your portfolio and your activity.",
  };
  if (!apps.length) return {
    tone: "neutral", label: "Almost ready", title: "Connect an AI app.", action: "guide",
    summary: `Your data is saved, last synced ${updated || "recently"}. Add it to an AI app to start asking about it.`,
  };
  const used = apps.map(app => app.last_used_at).filter(Boolean).sort().at(-1);
  return {
    tone: "success", label: "Ready", title: `${appNames(apps)} can read your portfolio.`, action: "sync",
    summary: `Data synced ${updated || "recently"}.` + (used ? ` Last used ${ago(used)}.` : ""),
  };
}

function renderStatus() {
  const portfolio = status.portfolio || {};
  const activities = status.activities || {};
  const apps = status.apps || [];
  const state = overallState(portfolio, activities, apps);
  el("overview-hero").dataset.tone = state.tone;
  el("status-label").textContent = state.label;
  el("status-title").textContent = state.title;
  el("status-summary").textContent = state.summary;
  el("primary-action").disabled = busy;  // a status reload mid-operation must not unlock it
  el("primary-action").dataset.action = state.action;
  el("primary-action").textContent = state.action === "guide" ? "Connect an AI app" : "Sync now";
  // When a sign-in is needed, the form itself is the one action on the page.
  const needsSignIn = ["connect", "reconnect"].includes(state.action);
  el("primary-action").classList.toggle("hidden", needsSignIn);
  setConnectMode(needsSignIn ? state.action : null);
  el("ws-signout").classList.toggle("hidden", portfolio.connection_state !== "connected");
  el("apps-list").replaceChildren(...(apps.length ? apps.map(appRow) : [emptyRow("No apps are connected yet.")]));

  el("portfolio-updated").textContent = portfolio.snapshot_available
    ? "Updated " + (ago(portfolio.fetched_at) || "at an unknown time") : "Not synced yet";
  if (!portfolio.snapshot_available) chip("portfolio-chip", "No data");
  else if (portfolio.last_sync_error) chip("portfolio-chip", "Needs attention", "warning");
  else chip("portfolio-chip", "Current", "success");

  const since = monthYear(activities.earliest_saved_occurred_at);
  el("activity-updated").textContent = activities.available
    ? count(activities.rows_processed) + " records" + (since ? " since " + since : "") : "Not synced yet";
  if (!activities.available) chip("activity-chip", "No data");
  else if (activities.error) chip("activity-chip", "Needs attention", "warning");
  else if (activities.coverage_complete === false) chip("activity-chip", "Partial", "warning");
  else chip("activity-chip", "Complete", "success");
  renderData();

  const diagnostics = status.diagnostics || {};
  el("mcp-url").textContent = diagnostics.mcp_endpoint || "—";
  // With several deployments, the project ID is how you tell this one apart.
  if (diagnostics.project_id) {
    el("project-id").textContent = diagnostics.project_id;
    el("project").classList.remove("hidden");
    document.title = diagnostics.project_id + " · AI connector for your Wealthsimple";
    renderResources(diagnostics);
  }
  el("fact-version").textContent = [diagnostics.connector_version, diagnostics.commit?.slice(0, 7),
    diagnostics.committed_on].filter(Boolean).join(" · ") || "—";
  if (diagnostics.repository) {
    const github = `https://github.com/${diagnostics.repository}`;
    el("source").href = diagnostics.commit ? `${github}/tree/${diagnostics.commit}` : github;
    el("source").classList.remove("hidden");
    el("fact-source").replaceChildren(external(github, diagnostics.repository + " ↗"));
  }
  checkForUpdate(diagnostics.connector_version, diagnostics.repository);
  el("fact-session").textContent = {connected: "Connected", not_connected: "Not connected",
    reconnect_required: "Signed out"}[portfolio.connection_state] || "Unknown";
  el("fact-sync").firstElementChild.textContent = portfolio.last_sync_error || portfolio.last_sync_status || "—";
  el("fact-activity").firstElementChild.textContent = activities.error || activities.sync_status || "—";
}

// Holdings: weights and returns by default; amounts only when asked for.
function holdings(positions) {
  const bySymbol = new Map();
  for (const position of positions) {
    const symbol = position.symbol || position.security_type || "Other";
    const item = bySymbol.get(symbol) || {symbol, value: 0, book: 0, gain: 0, hasBook: true,
      currency: position.reported_market_value?.currency};
    item.value += amount(position.reported_market_value) || 0;
    const book = amount(position.reported_book_value), gain = amount(position.reported_unrealized_returns);
    if (book === null || gain === null) item.hasBook = false;
    else { item.book += book; item.gain += gain; }
    bySymbol.set(symbol, item);
  }
  const items = [...bySymbol.values()].sort((a, b) => b.value - a.value);
  const total = items.reduce((sum, item) => sum + item.value, 0);
  // Weights only make sense when every value is in the same currency.
  const oneCurrency = new Set(items.map(item => item.currency)).size <= 1;
  return {items, total, currency: items[0]?.currency, oneCurrency};
}

const ACTIVITY_VERBS = {DIY_BUY: "Bought", MANAGED_BUY: "Bought", DIY_SELL: "Sold", MANAGED_SELL: "Sold",
  DIVIDEND: "Dividend", INTEREST: "Interest", DEPOSIT: "Deposit", WITHDRAWAL: "Withdrawal",
  INTERNAL_TRANSFER: "Transfer", SPEND: "Purchase", CREDIT_CARD: "Card"};
const words = text => (text || "").toLowerCase().replaceAll("_", " ").replace(/^./, c => c.toUpperCase());

function activityTitle(row) {
  const verb = ACTIVITY_VERBS[row.type] || words(row.sub_type || row.type) || "Activity";
  const subject = row.asset_symbol || row.merchant || row.aft_originator_name;
  return subject ? verb + " · " + subject : verb;
}

function renderData() {
  const {items, total, currency: code, oneCurrency} = holdings(status.positions || []);
  el("toggle-amounts").textContent = showAmounts ? "Hide amounts" : "Show amounts";
  el("toggle-amounts").setAttribute("aria-pressed", String(showAmounts));
  el("holdings-total").textContent = items.length ? currency(total, code) : "—";
  el("holdings-caption").textContent = items.length
    ? `across ${count(status.portfolio?.position_count)} positions` : "No holdings saved yet";
  const top = items.slice(0, 6).map(item => {
    const change = item.hasBook && item.book ? item.gain / item.book : null;
    const row = listItem(item.symbol, showAmounts ? currency(item.value, item.currency)
      : oneCurrency && total ? percent(item.value / total) + " of holdings" : "",
      percent(change, true), change > 0 ? "up" : change < 0 ? "down" : "");
    if (oneCurrency && total) row.style.setProperty("--weight", (item.value / total * 100).toFixed(1) + "%");
    return row;
  });
  if (items.length > 6) top.push(emptyItem(`and ${items.length - 6} more`));
  el("holdings").replaceChildren(...(top.length ? top : [emptyItem("Sync to see your holdings.")]));

  const recent = (status.recent_activities || []).map(row => {
    const value = amount(row);
    const sign = row.amount_sign === "negative" ? -1 : 1;
    return listItem(activityTitle(row),
      [row.account_nickname, shortDate(row.occurred_at)].filter(Boolean).join(" · "),
      value === null ? "" : currency(sign * value, row.currency),
      showAmounts && value !== null && sign > 0 ? "up" : "");
  });
  el("recent").replaceChildren(...(recent.length ? recent : [emptyItem("Sync to see recent activity.")]));
  fitRecent();
}

// Show as many recent rows as fit beside Holdings without making the tiles taller.
function fitRecent() {
  const rows = [...el("recent").children];
  const holdingsTile = el("holdings").closest(".tile"), recentTile = el("recent").closest(".tile");
  const least = holdingsTile.offsetTop === recentTile.offsetTop ? 3 : 5;
  rows.forEach((row, index) => { row.hidden = index >= least; });
  if (least === 5 || !holdingsTile.offsetHeight) return;
  const height = holdingsTile.offsetHeight;
  for (const row of rows.slice(least)) {
    row.hidden = false;
    if (holdingsTile.offsetHeight > height) { row.hidden = true; break; }
  }
}
window.addEventListener("resize", () => { if (status) fitRecent(); });

// Two clicks: the first asks, the second destroys the stored Wealthsimple session.
el("ws-signout").onclick = async () => {
  const button = el("ws-signout");
  if (!button.dataset.confirm) {
    button.dataset.confirm = "1";
    button.textContent = "Click again to sign out";
    return;
  }
  delete button.dataset.confirm;
  button.textContent = "Sign out of Wealthsimple";
  button.disabled = true;
  try {
    const {result} = await api("/owner/signout");
    await loadStatus();
    say("message", result === "signed_out"
      ? "Signed out of Wealthsimple. Your saved data stays; sign in again any time."
      : "Couldn't sign out. Try again.", result === "signed_out" ? "success" : "danger");
    window.scrollTo({top: 0, behavior: "smooth"});
  } catch (error) {
    if (error instanceof OwnerSessionExpired) return expireSession();
    say("message", "Couldn't sign out. Try again.", "danger");
  } finally {
    button.disabled = false;
  }
};

el("toggle-amounts").onclick = () => {
  showAmounts = !showAmounts;
  if (status) renderData();
};

async function loadStatus() {
  status = await api("/owner/status");
  renderStatus();
}

function setConnectMode(mode) {
  if (mode === connectMode) return;
  connectMode = mode;
  el("connect-panel").classList.toggle("hidden", !mode);
  resetConnectForm();
}

function resetConnectForm() {
  el("connect-form").reset();
  el("credential-fields").classList.remove("hidden");
  el("otp-field").classList.add("hidden");
  el("otp").required = false;
  el("connect-submit").textContent = "Continue";
}

// One operation at a time: sign-in, sync and sign-out controls stay disabled until it ends.
function setBusy(value) {
  busy = value;
  for (const id of ["primary-action", "connect-submit", "refresh-portfolio", "refresh-activities",
    "ws-signout"])
    el(id).disabled = value;
}

async function sync(targets, messageId = "message") {
  setBusy(true);
  let last = "refresh_succeeded";
  try {
    for (const target of targets) {
      say(messageId, target === "portfolio" ? "Syncing your portfolio…" : "Syncing your activity…");
      // A long first activity sync runs in rounds of about three minutes; keep going until done.
      for (let round = 0; round < 30; round++) {
        const result = await api("/owner/refresh", {target});
        last = result.result;
        if (last !== "refresh_continues") break;
        const since = monthYear(result.earliest_occurred_at);
        say(messageId, `Saving your activity history: ${count(result.rows_processed)} so far`
          + (since ? `, back to ${since}…` : "…"));
      }
      if (!["refresh_succeeded", "refresh_cooldown", "refresh_partial"].includes(last)) break;
    }
    await loadStatus();
    say(messageId, ...(REFRESH_COPY[last] || REFRESH_COPY.refresh_result_unknown));
  } catch (error) {
    if (error instanceof OwnerSessionExpired) return expireSession();
    say(messageId, "The sync couldn't start. Your saved data is unchanged.", "danger");
  } finally {
    setBusy(false);
  }
}

el("primary-action").onclick = () => {
  const action = el("primary-action").dataset.action;
  if (action === "guide") el("apps").scrollIntoView({behavior: "smooth"});
  else sync(["portfolio", "activities"]);
};

el("connect-form").onsubmit = async event => {
  event.preventDefault();
  setBusy(true);
  let syncNext = false;
  say("message", "Signing in to Wealthsimple…");
  try {
    const reply = await api("/owner/reconnect", {username: el("username").value,
      password: el("password").value, otp: el("otp").value || null});
    const result = reply.result;
    if (result === "mfa_required") {
      el("otp-hint").textContent = otpHint(reply);
      el("credential-fields").classList.add("hidden");
      el("otp-field").classList.remove("hidden");
      el("otp").required = true;
      el("otp").focus();
      el("connect-submit").textContent = "Verify";
      say("message", "");
    } else if (result === "reconnect_succeeded") {
      resetConnectForm();
      await loadStatus();
      syncNext = true;
    } else {
      if (result === "login_rejected") resetConnectForm();
      say("message", CONNECT_COPY[result] || "Wealthsimple sign-in is unavailable right now.", "danger");
    }
  } catch (error) {
    if (error instanceof OwnerSessionExpired) return expireSession();
    el("password").value = "";
    say("message", "Sign-in couldn't be completed. Nothing was saved.", "danger");
  } finally {
    if (!syncNext) setBusy(false);
  }
  // Start syncing without another click: the first time nothing is saved yet, and after a
  // reconnect the saved data stopped updating while signed out. The controls stay disabled
  // from sign-in through the sync, which shows its progress and releases them once at the end.
  if (syncNext) await sync(["portfolio", "activities"]);
};

// ---------- AI apps ----------

function appName(app) {
  // Gemini registers under the name "Google"; its callback host is clearer.
  if (app.redirect_host === "oauth-redirect.googleusercontent.com") return "Gemini";
  if (app.name) return app.name;
  if (app.redirect_host === "chatgpt.com") return "ChatGPT";
  if (app.redirect_host === "claude.ai" || app.redirect_host === "claude.com") return "Claude";
  return app.redirect_host || "Unknown app";
}

function emptyRow(text) {
  const row = document.createElement("li");
  row.className = "row muted";
  row.textContent = text;
  return row;
}

function appRow(app) {
  const row = document.createElement("li");
  row.className = "row";
  const name = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = appName(app);
  const detail = document.createElement("span");
  const connected = monthDay(app.connected_at);
  detail.textContent = [connected ? "Connected " + connected : "Connected",
    app.last_used_at && "Last used " + ago(app.last_used_at)].filter(Boolean).join(" · ");
  name.append(title, detail);
  const button = document.createElement("button");
  button.type = "button";
  button.className = "link-button danger";
  button.textContent = "Disconnect";
  // Two-step: the first click asks, the second one disconnects.
  button.onclick = async () => {
    if (!button.dataset.confirm) {
      button.dataset.confirm = "1";
      button.textContent = "Confirm disconnect";
      return;
    }
    button.disabled = true;
    try {
      await api("/owner/apps/disconnect", {client_id: app.client_id});
      await loadStatus();
    } catch (error) {
      if (error instanceof OwnerSessionExpired) return expireSession();
      button.textContent = "Try again";
      button.disabled = false;
    }
  };
  row.append(name, button);
  return row;
}

async function copyText(value, button) {
  try {
    await navigator.clipboard.writeText(value);
    button.textContent = "Copied";
  } catch {
    button.textContent = "Select and copy";
  }
  setTimeout(() => { button.textContent = "Copy"; }, 2000);
}
el("copy-url").onclick = event => copyText(el("mcp-url").textContent, event.currentTarget);

function copyField(value) {
  const field = document.createElement("div");
  field.className = "copy-field";
  const code = document.createElement("code");
  code.textContent = value;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "pill small";
  button.textContent = "Copy";
  button.onclick = () => copyText(value, button);
  field.append(code, button);
  return field;
}

function external(href, text, className = "") {
  const link = document.createElement("a");
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener";
  link.className = className;
  link.textContent = text;
  return link;
}

// Where Gemini Spark lists and adds custom apps, checked September 2026.
const GEMINI_APPS = "https://gemini.google.com/spark/apps";
// Where ChatGPT lists and manages custom plugins, checked September 2026.
const CHATGPT_PLUGINS = "https://chatgpt.com/plugins";
// Where Claude lists and manages connectors, checked September 2026.
const CLAUDE_CONNECTORS = "https://claude.ai/customize/connectors";

// A step is text, optionally with a value to copy or a link to open.
function guides() {
  const d = status?.diagnostics || {};
  return {
    chatgpt: {title: "Connect ChatGPT", steps: [
      {text: "Open Plugins in ChatGPT on the web.",
        href: CHATGPT_PLUGINS, label: "Open ChatGPT Plugins"},
      "Choose Add, then Create MCP App. Name it Wealthsimple.",
      {text: "Under Connection, keep Server URL and paste this address. Leave Authentication on OAuth.",
        copy: d.mcp_endpoint},
      "Tick I understand and want to continue, then choose Create.",
      "When a sign-in window opens, choose this Google account and allow access.",
      "On the plugin's page, choose Try in chat. Later, type @Wealthsimple in any chat to use it.",
    ], note: "No Create MCP App under Add? Turn on Developer mode in ChatGPT's settings first. ChatGPT renames its menus from time to time; look for the closest match."},
    claude: {title: "Connect Claude", steps: [
      {text: "Open Connectors in Claude on the web.",
        href: CLAUDE_CONNECTORS, label: "Open Claude Connectors"},
      "Choose +, then Add custom connector. Name it Wealthsimple.",
      {text: "Paste this address as the server URL, then continue.", copy: d.mcp_endpoint},
      "Keep the detected settings: Sign in now, and Register automatically. Leave Request headers empty, then choose Add.",
      "When a sign-in window opens, choose this Google account and allow access.",
      "In a chat, make sure Wealthsimple is turned on under + and Connectors, then ask about your portfolio.",
    ], note: "Claude renames its menus from time to time; look for the closest match."},
    gemini: {title: "Connect Gemini", steps: [
      {text: "Open Apps in Gemini Spark on the web.",
        href: GEMINI_APPS, label: "Open Gemini Spark apps"},
      {text: "Choose Add a custom app, and paste this address as the MCP server URL.", copy: d.mcp_endpoint},
      "Leave Client ID and Client secret empty, then continue.",
      "When a sign-in window opens, choose this Google account and allow access.",
      "In a Spark task, type @ and choose Wealthsimple, then ask about your portfolio.",
    ], note: "Gemini renames its menus from time to time; look for the closest match."},
    update: {title: `Update to ${latestRelease?.version}`, steps: [
      {text: "Open Cloud Shell with the latest code. Sign in with the Google account that owns this project if asked.",
        // The stable branch always holds the latest release, never unreleased work on main.
        href: `https://shell.cloud.google.com/cloudshell/editor?cloudshell_git_repo=https://github.com/${d.repository}&cloudshell_git_branch=stable&show=terminal`,
        label: "Open Cloud Shell"},
      {text: "Paste this into the terminal and press Enter:",
        copy: `firebase/scripts/bootstrap.sh ${d.project_id} ${user?.email}`},
      "Type y when asked. It takes about 10 minutes. Your data, Wealthsimple sign-in and AI app connections stay as they are.",
    ], note: "Updating only deploys new code into your own project. You can read every change first under What's new."},
  };
}

function openGuide(name) {
  const guide = guides()[name];
  el("guide-title").textContent = guide.title;
  const body = [];
  const section = steps => {
    if (!steps.length) return;
    const list = document.createElement("ol");
    list.className = "steps";
    for (const step of steps) {
      const {text, copy, href, label} = typeof step === "string" ? {text: step} : step;
      const item = document.createElement("li");
      item.append(text);
      if (copy) item.append(copyField(copy));
      if (href) item.append(external(href, label, "pill small step-link"));
      list.append(item);
    }
    body.push(list);
  };
  section(guide.steps);
  const note = document.createElement("p");
  note.className = "panel-note";
  note.textContent = guide.note;
  body.push(note);
  el("guide-body").replaceChildren(...body);
  el("guide").showModal();
}

for (const button of document.querySelectorAll("[data-guide]"))
  button.onclick = () => openGuide(button.dataset.guide);
// Clicking the dimmed backdrop closes the sheet.
el("guide").onclick = event => { if (event.target === el("guide")) el("guide").close(); };

// ---------- Updates ----------

let latestRelease = null;

function isNewer(candidate, current) {
  const [a, b] = [candidate, current].map(v => v.split(".").map(Number));
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i];
  return false;
}

// Asks GitHub from your browser, once per page load; nothing is sent about you or your data.
async function checkForUpdate(current, repository) {
  if (latestRelease || !repository || !/^\d+\.\d+\.\d+$/.test(current || "")) return;
  try {
    const response = await fetch(`https://api.github.com/repos/${repository}/releases/latest`);
    const release = response.ok ? await response.json() : null;
    const version = release?.tag_name?.replace(/^v/, "");
    if (!version || !/^\d+\.\d+\.\d+$/.test(version) || !isNewer(version, current)) return;
    latestRelease = {version, url: release.html_url};
    el("update-text").textContent = `Version ${version} is available. You're on ${current}.`;
    el("update-notes").href = release.html_url;
    el("update").classList.remove("hidden");
  } catch {
    // Offline or rate-limited: simply no banner.
  }
}
el("update-how").onclick = () => openGuide("update");

// ---------- Your Firebase project ----------

function renderResources(d) {
  const project = encodeURIComponent(d.project_id);
  const firebase = `https://console.firebase.google.com/project/${project}`;
  const cloud = path => `https://console.cloud.google.com/${path}?project=${project}`;
  const database = id => `${firebase}/firestore/databases/${id === "(default)" ? "-default-" : id}/data`;
  // The Cloud Run address is <service>-<project number>.<region>.run.app.
  const [label, region] = new URL(d.mcp_endpoint).hostname.split(".");
  const service = label.replace(/-\d+$/, "");
  const rows = [
    ["Your saved data", "Firestore database with your portfolio and activity", database(d.portfolio_database)],
    ["Functions", "Private sync, sign-in, and a twice-daily sign-in refresh", `${firebase}/functions`],
    ["This console and MCP", "The Cloud Run service your AI apps talk to", cloud(`run/detail/${region}/${service}/metrics`)],
    ["Wealthsimple session", "Secret Manager; only the private functions can read it", cloud("security/secret-manager")],
    ["Logs", "What every part has been doing", cloud("logs/query")],
    ["Usage and billing", "What this project costs on the Blaze plan", `${firebase}/usage`],
  ];
  el("resources").replaceChildren(...rows.map(([title, detail, href]) => {
    const row = document.createElement("li");
    const link = external(href, "");
    const strong = document.createElement("strong");
    strong.textContent = title;
    const span = document.createElement("span");
    span.textContent = detail;
    link.append(strong, span);
    row.append(link);
    return row;
  }));
  el("project").href = `${firebase}/overview`;
  el("open-firestore").href = database(d.portfolio_database);
}

// ---------- Developer ----------

let previewTarget = "portfolio";

async function preview(target) {
  previewTarget = target;
  for (const name of ["portfolio", "activities"])
    el("preview-" + name).setAttribute("aria-pressed", String(name === target));
  const output = el("preview-output");
  output.textContent = "Loading…";
  try {
    output.textContent = JSON.stringify(await api("/owner/preview", {target}), null, 2);
  } catch (error) {
    if (error instanceof OwnerSessionExpired) return expireSession();
    output.textContent = error.message === "no_snapshot" || error.message === "no_activity_snapshot"
      ? "Nothing is saved yet. Run a sync first." : "The preview couldn't be loaded.";
  }
}

el("preview-portfolio").onclick = () => preview("portfolio");
el("preview-activities").onclick = () => preview("activities");
el("refresh-portfolio").onclick = () => sync(["portfolio"], "developer-message");
el("refresh-activities").onclick = () => sync(["activities"], "developer-message");

// ---------- Sign-in ----------

async function signedIn(value) {
  user = value;
  el("identity").textContent = user.email;
  showSignedIn(true);
  try {
    await loadStatus();
  } catch (error) {
    // The first request is also where a non-owner Google account is turned away.
    if (error instanceof OwnerSessionExpired)
      return expireSession("Sign in with the Google account that owns this deployment.");
    say("message", "Your connector's status couldn't be loaded. Try reloading.", "danger");
  }
}

async function expireSession(text = "Your sign-in expired. Sign in again to continue.") {
  await signOut(auth);
  say("signin-message", text);
}

function signedOut() {
  user = status = null;
  connectMode = null;
  el("connect-panel").classList.add("hidden");
  el("project").classList.add("hidden");
  showSignedIn(false);
  el("signin").disabled = false;
}

try {
  const config = await fetch("/firebase-config").then(response => {
    if (!response.ok) throw new Error();
    return response.json();
  });
  auth = getAuth(initializeApp(config));
  // Session persistence: survives a reload of this tab, cleared when the tab closes.
  await setPersistence(auth, browserSessionPersistence);
  onAuthStateChanged(auth, value => {
    if (value) signedIn(value);
    else {
      signedOut();
      if (el("signin-message").textContent === "Loading…") say("signin-message", "");
    }
  });
} catch {
  say("signin-message", "Sign-in is unavailable. Check this deployment's Firebase setup.", "danger");
}

el("signin").onclick = async () => {
  el("signin").disabled = true;
  say("signin-message", "Opening Google sign-in…");
  try {
    const provider = new GoogleAuthProvider();
    provider.setCustomParameters({prompt: "select_account"});
    await signInWithPopup(auth, provider);
    say("signin-message", "");
  } catch {
    say("signin-message", "Sign-in wasn't completed.");
    el("signin").disabled = false;
  }
};

el("signout").onclick = () => signOut(auth).then(() => say("signin-message", "Signed out."));
window.addEventListener("hashchange", () => { if (user) showView(); });
