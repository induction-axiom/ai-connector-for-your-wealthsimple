import {initializeApp} from "https://www.gstatic.com/firebasejs/12.3.0/firebase-app.js";
import {getAuth, GoogleAuthProvider, signInWithPopup, inMemoryPersistence, setPersistence, signOut}
  from "https://www.gstatic.com/firebasejs/12.3.0/firebase-auth.js";

const el = id => document.getElementById(id);
const ticket = new URLSearchParams(location.hash.slice(1)).get("request");
history.replaceState(null, "", "/consent");
let csrf, auth, user;
async function post(path, value) {
  const r = await fetch(path, {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)});
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || "request_failed");
  return data;
}
function fail() {
  el("status").textContent = "That didn't work. Sign in with the Google account that owns this connector, or start connecting again from your AI app.";
}
try {
  if (!ticket) throw new Error("missing_request");
  const [config, view] = await Promise.all([
    fetch("/firebase-config").then(r => r.json()), post("/consent/start", {request: ticket})]);
  csrf = view.csrf;
  const host = new URL(view.redirect_uri).hostname;
  if (host === "oauth-redirect.googleusercontent.com") el("app-name").textContent = "Gemini";
  if (host === "chatgpt.com") el("app-name").textContent = "ChatGPT";
  if (host === "claude.ai" || host === "claude.com") el("app-name").textContent = "Claude";
  el("description").textContent = "Your AI app is asking to connect to your own private AI connector for Wealthsimple.";
  el("scope").textContent = "Read your accounts, holdings and activity, and ask for fresh data from Wealthsimple. " +
    "It can't trade, move money, or see your Wealthsimple password. It stays connected while you use it; " +
    "after 3 months unused, it asks you again.";
  el("callback").textContent = host;
  el("client").textContent = view.redirect_uri;
  auth = getAuth(initializeApp(config));
  await setPersistence(auth, inMemoryPersistence);
  el("signin").disabled = false;
  el("status").textContent = "";
} catch { fail(); }

el("signin").onclick = async () => {
  try {
    const provider = new GoogleAuthProvider();
    provider.setCustomParameters({prompt: "select_account"});
    user = (await signInWithPopup(auth, provider)).user;
    el("identity").textContent = "Signed in as " + user.email;
    el("signin").classList.add("hidden");
    el("signin-note").classList.add("hidden");
    el("decision").classList.remove("hidden");
    el("approve").disabled = el("deny").disabled = false;
    el("status").textContent = "Choose Allow or Don't allow.";
  } catch { fail(); }
};
for (const decision of ["approve", "deny"]) {
  el(decision).onclick = async () => {
    el("approve").disabled = el("deny").disabled = true;
    try {
      const value = await post("/consent/finish", {request: ticket, csrf, decision,
        id_token: await user.getIdToken(true)});
      await signOut(auth);
      location.assign(value.redirect);
    } catch { fail(); }
  };
}
