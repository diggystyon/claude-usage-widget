// Claude Usage Bridge - service worker.
//
// Every 60 seconds (and on browser startup), fetch
// /api/organizations/{org}/usage from claude.ai using the user's existing
// session, then POST the JSON to the local widget at 127.0.0.1:38080.
//
// No tab needs to be open. As long as the browser process is alive and the
// user is signed into claude.ai, the bridge keeps running silently.

const WIDGET_ENDPOINT = "http://127.0.0.1:38080/usage";
const POLL_MINUTES = 1;

async function getOrgId() {
  const cached = (await chrome.storage.local.get("orgId")).orgId;
  if (cached) return cached;
  const r = await fetch("https://claude.ai/api/organizations", { credentials: "include" });
  if (!r.ok) throw new Error("organizations -> " + r.status);
  const orgs = await r.json();
  const orgId = orgs && orgs[0] && (orgs[0].uuid || orgs[0].id);
  if (orgId) await chrome.storage.local.set({ orgId });
  return orgId;
}

function setBadge(text, color) {
  try {
    if (color) chrome.action.setBadgeBackgroundColor({ color });
    chrome.action.setBadgeText({ text: text || "" });
  } catch (_) { /* ignore - some browsers may not support */ }
}

async function fetchAndPost() {
  let orgId;
  try {
    orgId = await getOrgId();
  } catch (e) {
    setBadge("?", "#aa6633");
    return;
  }
  if (!orgId) {
    setBadge("?", "#aa6633");
    return;
  }
  let usage;
  try {
    const r = await fetch(`https://claude.ai/api/organizations/${orgId}/usage`, { credentials: "include" });
    if (!r.ok) {
      setBadge("!", "#cc4444");
      return;
    }
    usage = await r.json();
  } catch (e) {
    setBadge("!", "#cc4444");
    return;
  }
  try {
    await fetch(WIDGET_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(usage),
    });
    setBadge("");
  } catch (e) {
    // Widget not running on this machine. Silent - user may not have it installed.
    setBadge("");
  }
}

function ensureAlarm() {
  chrome.alarms.get("poll", (a) => {
    if (!a) {
      chrome.alarms.create("poll", {
        periodInMinutes: POLL_MINUTES,
        delayInMinutes: 0.05,
      });
    }
  });
}

chrome.runtime.onInstalled.addListener(() => {
  ensureAlarm();
  fetchAndPost();
});

chrome.runtime.onStartup.addListener(() => {
  ensureAlarm();
  fetchAndPost();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "poll") fetchAndPost();
});
