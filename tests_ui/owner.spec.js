// Synthetic data only: the page is served from firebase/mcp/static, with Google sign-in
// and every /owner API replaced, so no deployment or account is touched.
import {readFileSync} from "node:fs";
import {test, expect} from "@playwright/test";

const STATIC = new URL("../firebase/mcp/static/", import.meta.url);
const BASE = "https://owner.test";
const CONTROLS = ["primary-action", "connect-submit", "refresh-portfolio", "refresh-activities",
  "ws-signout"];

const FIREBASE_APP = "export const initializeApp = config => ({config});";
const OWNER = "owner@example.com";
// Signed in as `email` on load; signing out tells the page, as Firebase does.
const firebaseAuth = email => `
  let listener;
  export const getAuth = app => ({app});
  export class GoogleAuthProvider { setCustomParameters() {} }
  export const browserSessionPersistence = "session";
  export const setPersistence = async () => {};
  export const signInWithPopup = async () => {};
  export const signOut = async () => listener(null);
  export const onAuthStateChanged = (auth, callback) => {
    listener = callback;
    setTimeout(() => callback({email: "${email}", getIdToken: async () => "synthetic-token"}));
  };
`;

function status(connection) {
  const connected = connection === "connected";
  return {
    portfolio: {snapshot_available: true, connection_state: connection,
      reconnect_required: connection === "reconnect_required",
      fetched_at: "2026-09-27T12:00:00+00:00", account_count: 1, position_count: 0},
    activities: {available: true, coverage_complete: connected, sync_status: "complete",
      fetched_at: "2026-09-27T12:00:00+00:00", rows_processed: 1},
    positions: [], recent_activities: [], apps: [],
    diagnostics: {connector_version: "test", repository: "example/example",
      mcp_endpoint: BASE + "/mcp"},
  };
}

// Serve the dashboard; each /owner/refresh waits until the test settles it. Sign-in replies
// with each of `challenges` first, then succeeds. stale: the owner's sign-in is older than the
// server accepts.
async function openConsole(page, extra = {}, challenges = [], email = OWNER, stale = false) {
  const state = {connection: "reconnect_required", refreshes: [], errors: [], reconnects: []};
  page.on("pageerror", error => state.errors.push(error.message));
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    const json = body => route.fulfill({contentType: "application/json", body: JSON.stringify(body)});
    if (url.hostname === "www.gstatic.com")
      return route.fulfill({contentType: "text/javascript",
        body: url.pathname.endsWith("firebase-auth.js") ? firebaseAuth(email) : FIREBASE_APP});
    if (url.origin !== BASE) return route.fulfill({status: 404, body: ""});
    if (url.pathname === "/owner")
      return route.fulfill({contentType: "text/html", body: readFileSync(new URL("owner.html", STATIC))});
    if (url.pathname.startsWith("/assets/")) {
      const name = url.pathname.slice("/assets/".length);
      return route.fulfill({contentType: name.endsWith(".js") ? "text/javascript" : "text/css",
        body: readFileSync(new URL(name, STATIC))});
    }
    if (url.pathname === "/firebase-config") return json({authDomain: "example.test"});
    if (url.pathname === "/owner/status" && (email !== OWNER || stale)) return new Promise(settle => {
      state.turnAway = () => route.fulfill({status: 403, contentType: "application/json",
        body: JSON.stringify({error: stale ? "owner_login_required" : "not_owner"})}).then(settle);
    });
    if (url.pathname === "/owner/status") return json({...status(state.connection), ...extra});
    if (url.pathname === "/owner/reconnect/resend") {
      state.resends = (state.resends || 0) + 1;
      return json({result: "login_challenge_resent"});
    }
    if (url.pathname === "/owner/reconnect" || url.pathname === "/owner/reconnect/approval") {
      state.reconnects.push({path: url.pathname, ...route.request().postDataJSON()});
      if (challenges.length) return json(challenges.shift());
      state.connection = "connected";
      return json({result: "reconnect_succeeded"});
    }
    if (url.pathname === "/owner/refresh") {
      const {target} = route.request().postDataJSON();
      return new Promise(settle => state.refreshes.push(
        {target, reply: result => { json({result}); settle(); }}));
    }
    return json({});
  });
  await page.goto(BASE + "/owner");
  return state;
}

const disabled = page => page.evaluate(ids => Object.fromEntries(
  ids.map(id => [id, document.getElementById(id).disabled])), CONTROLS);

async function nextRefresh(state, target) {
  await expect.poll(() => state.refreshes.map(r => r.target)).toContain(target);
  return state.refreshes.find(r => r.target === target);
}

async function reconnect(page) {
  await page.fill("#username", "owner@example.com");
  await page.fill("#password", "synthetic-password");
  await page.click("#connect-submit");
}

test("controls stay disabled from reconnect until the automatic sync finishes", async ({page}) => {
  const state = await openConsole(page);
  await reconnect(page);

  const portfolio = await nextRefresh(state, "portfolio");
  const locked = Object.fromEntries(CONTROLS.map(id => [id, true]));
  expect(await disabled(page)).toEqual(locked);
  await expect(page.locator("#message")).toHaveText("Syncing your portfolio…");
  portfolio.reply("refresh_succeeded");

  const activities = await nextRefresh(state, "activities");
  expect(await disabled(page)).toEqual(locked);
  await expect(page.locator("#message")).toHaveText("Syncing your activity…");
  activities.reply("refresh_succeeded");

  await expect(page.locator("#message")).toHaveText("Your data is up to date.");
  await expect(page.locator("#primary-action")).toBeEnabled();
  await expect(page.locator("#refresh-portfolio")).toBeEnabled();
  expect(state.errors).toEqual([]);
});

test("the code prompt names where Wealthsimple sent the code", async ({page}) => {
  const state = await openConsole(page, {}, [
    {result: "mfa_required", method: "sms", hint: "1234"}]);
  await reconnect(page);
  await expect(page.locator("#otp-hint")).toHaveText(
    "Enter the code Wealthsimple texted to the number ending in 1234.");
  await expect(page.locator("#credential-fields")).toBeHidden();
  expect(state.errors).toEqual([]);
});

test("a sign-in waiting for approval in the Wealthsimple app continues on its own once approved", async ({page}) => {
  await page.clock.install();
  const state = await openConsole(page, {}, [
    {result: "mfa_required", method: "app", hint: null},
    {result: "login_challenge_required"},
    {result: "login_challenge_pending"}]);
  await reconnect(page);
  await page.fill("#otp", "123456");
  await page.click("#connect-submit");

  await expect(page.locator("#connect-submit")).toHaveText("Waiting for approval…");
  await expect(page.locator("#connect-submit")).toBeDisabled();
  await expect(page.locator("#credential-fields")).toBeHidden();
  await expect(page.locator("#otp-field")).toBeHidden();
  await expect(page.locator("#message")).toContainText("tap Yes; this page continues on its own");

  // No more clicks: the page asks again until the approval comes through, then syncs.
  await page.clock.runFor(3000);
  await expect.poll(() => state.reconnects.length).toBe(3);
  await page.clock.runFor(3000);
  (await nextRefresh(state, "portfolio")).reply("refresh_succeeded");
  const [first, code, ...approvals] = state.reconnects;
  expect(first).toMatchObject({path: "/owner/reconnect", otp: null});
  expect(code).toMatchObject({path: "/owner/reconnect", otp: "123456"});
  expect(approvals).toHaveLength(2);
  for (const approval of approvals)
    expect(approval).toEqual({path: "/owner/reconnect/approval",
      username: "owner@example.com", password: "synthetic-password"});
  await expect(page.locator("#resend-approval")).toBeHidden();
  expect(state.errors).toEqual([]);
});

test("the approval request can be sent again, once every 30 seconds", async ({page}) => {
  await page.clock.install();
  const state = await openConsole(page, {}, [{result: "login_challenge_required"},
    ...Array(20).fill({result: "login_challenge_pending"})]);
  await reconnect(page);
  const resend = page.locator("#resend-approval");
  await expect(resend).toBeVisible();
  await resend.click();
  await expect(page.locator("#message")).toHaveText(
    "Notification sent again. Open it on your trusted device and tap Yes.");
  await expect(resend).toBeDisabled();
  await expect(resend).toHaveText(/Resend notification \(\d+s\)/);
  expect(state.resends).toBe(1);
  await page.clock.runFor(31000);
  await expect(resend).toBeEnabled();
  await expect(resend).toHaveText("Resend notification");
  expect(state.errors).toEqual([]);
});

test("a denied approval starts the sign-in over", async ({page}) => {
  await page.clock.install();
  const state = await openConsole(page, {}, [
    {result: "login_challenge_required"}, {result: "login_challenge_denied"}]);
  await reconnect(page);
  await expect(page.locator("#connect-submit")).toHaveText("Waiting for approval…");
  await page.clock.runFor(3000);
  await expect(page.locator("#message")).toHaveText(
    "The sign-in was denied in the Wealthsimple app. Sign in again to start over.");
  await expect(page.locator("#credential-fields")).toBeVisible();
  await expect(page.locator("#connect-submit")).toHaveText("Continue");
  await expect(page.locator("#connect-submit")).toBeEnabled();
  await expect(page.locator("#resend-approval")).toBeHidden();
  await reconnect(page);
  await expect.poll(() => state.reconnects.length).toBe(3);
  expect(state.reconnects.at(-1).path).toBe("/owner/reconnect");
  expect(state.errors).toEqual([]);
});

test("the code prompt stays neutral when the method is unknown", async ({page}) => {
  const state = await openConsole(page, {}, [{result: "mfa_required", method: null, hint: null}]);
  await reconnect(page);
  await expect(page.locator("#otp-hint")).toHaveText(
    "Enter the code from your authenticator app, or the one Wealthsimple texted or emailed you.");
  expect(state.errors).toEqual([]);
});

test("a failed automatic sync reports the result and releases the controls", async ({page}) => {
  const state = await openConsole(page);
  await reconnect(page);

  (await nextRefresh(state, "portfolio")).reply("refresh_failed");
  await expect(page.locator("#message")).toHaveText(
    "The sync didn't finish. Your saved data is unchanged.");
  await expect(page.locator("#primary-action")).toBeEnabled();
  expect(state.refreshes.map(r => r.target)).toEqual(["portfolio"]);  // stopped after the failure
  expect(state.errors).toEqual([]);
});

test("the ChatGPT guide links to Plugins and offers the address to paste", async ({page}) => {
  const state = await openConsole(page);
  await expect(page.locator("#mcp-url")).toHaveText(BASE + "/mcp");
  await page.evaluate(() => document.querySelector('[data-guide="chatgpt"]').click());
  const guide = page.locator("#guide");
  await expect(guide).toBeVisible();
  await expect(guide).toContainText("Create MCP App");
  await expect(guide).toContainText(BASE + "/mcp");
  const links = await guide.locator("a.step-link").evaluateAll(
    anchors => anchors.map(a => [a.textContent, a.href, a.target]));
  expect(links).toContainEqual(["Open ChatGPT Plugins", "https://chatgpt.com/plugins", "_blank"]);
  expect(state.errors).toEqual([]);
});

test("recent activity fills the height beside holdings, and no more", async ({page}) => {
  const money = amount => ({amount, currency: "CAD"});
  const positions = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"].map((symbol, i) => ({
    symbol, reported_market_value: money(String(100 - i)), reported_book_value: money("90"),
    reported_unrealized_returns: money(String(10 - i))}));
  const recent_activities = Array.from({length: 12}, (_, i) => ({
    type: "DIVIDEND", asset_symbol: "AAA", amount: "1.00", currency: "CAD",
    occurred_at: `2026-09-${String(20 - i).padStart(2, "0")}T12:00:00+00:00`}));
  await page.setViewportSize({width: 1280, height: 900});
  const state = await openConsole(page, {positions, recent_activities});
  await expect(page.locator("#holdings li")).toHaveCount(7);
  const heights = () => page.evaluate(() => ["holdings", "recent"].map(id =>
    document.getElementById(id).closest(".tile").getBoundingClientRect().height));
  const shown = page.locator("#recent li:not([hidden])");
  await expect.poll(async () => shown.count()).toBeGreaterThan(3);
  expect(await shown.count()).toBeLessThan(12);
  // One more row would make the Recent activity tile taller than Holdings.
  const [holdings, recent] = await heights();
  expect(recent).toBe(holdings);
  await page.evaluate(() => {
    const hidden = document.querySelector("#recent li[hidden]");
    hidden.hidden = false;
  });
  const [, taller] = await heights();
  expect(taller).toBeGreaterThan(holdings);
  expect(state.errors).toEqual([]);
});

test("another Google account is turned away before the dashboard shows", async ({page}) => {
  const state = await openConsole(page, {}, [], "someone@example.com");
  await expect(page.locator("#signin-message")).toHaveText("Checking your account…");
  await expect.poll(() => typeof state.turnAway).toBe("function");
  await expect(page.locator("#overview-view")).toBeHidden();
  await expect(page.locator("#account")).toBeHidden();
  await state.turnAway();
  await expect(page.locator("#signin-message")).toHaveText(
    "someone@example.com doesn't own this deployment. Sign in with the account that does.");
  await expect(page.locator("#signin-message")).toHaveAttribute("data-tone", "danger");
  await expect(page.locator("#signin")).toBeEnabled();
  await expect(page.locator("#overview-view")).toBeHidden();
  expect(state.errors).toEqual([]);
});

test("the owner's sign-in grown old asks to sign in again, not that they don't own it", async ({page}) => {
  const state = await openConsole(page, {}, [], OWNER, true);
  await expect.poll(() => typeof state.turnAway).toBe("function");
  await state.turnAway();
  await expect(page.locator("#signin-message")).toHaveText("Your sign-in expired. Sign in again to continue.");
  await expect(page.locator("#signin")).toBeEnabled();
  await expect(page.locator("#overview-view")).toBeHidden();
  expect(state.errors).toEqual([]);
});
