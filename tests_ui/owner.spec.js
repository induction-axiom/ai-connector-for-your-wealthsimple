// Synthetic data only: the page is served from firebase/mcp/static, with Google sign-in
// and every /owner API replaced, so no deployment or account is touched.
import {readFileSync} from "node:fs";
import {test, expect} from "@playwright/test";

const STATIC = new URL("../firebase/mcp/static/", import.meta.url);
const BASE = "https://owner.test";
const CONTROLS = ["primary-action", "connect-submit", "refresh-portfolio", "refresh-activities",
  "ws-signout"];

const FIREBASE_APP = "export const initializeApp = config => ({config});";
const FIREBASE_AUTH = `
  export const getAuth = app => ({app});
  export class GoogleAuthProvider { setCustomParameters() {} }
  export const browserSessionPersistence = "session";
  export const setPersistence = async () => {};
  export const signInWithPopup = async () => {};
  export const signOut = async () => {};
  export const onAuthStateChanged = (auth, callback) => setTimeout(() => callback(
    {email: "owner@example.com", getIdToken: async () => "synthetic-owner-token"}));
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
// with each of `challenges` first, then succeeds.
async function openConsole(page, extra = {}, challenges = []) {
  const state = {connection: "reconnect_required", refreshes: [], errors: []};
  page.on("pageerror", error => state.errors.push(error.message));
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    const json = body => route.fulfill({contentType: "application/json", body: JSON.stringify(body)});
    if (url.hostname === "www.gstatic.com")
      return route.fulfill({contentType: "text/javascript",
        body: url.pathname.endsWith("firebase-auth.js") ? FIREBASE_AUTH : FIREBASE_APP});
    if (url.origin !== BASE) return route.fulfill({status: 404, body: ""});
    if (url.pathname === "/owner")
      return route.fulfill({contentType: "text/html", body: readFileSync(new URL("owner.html", STATIC))});
    if (url.pathname.startsWith("/assets/")) {
      const name = url.pathname.slice("/assets/".length);
      return route.fulfill({contentType: name.endsWith(".js") ? "text/javascript" : "text/css",
        body: readFileSync(new URL(name, STATIC))});
    }
    if (url.pathname === "/firebase-config") return json({authDomain: "example.test"});
    if (url.pathname === "/owner/status") return json({...status(state.connection), ...extra});
    if (url.pathname === "/owner/reconnect") {
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
