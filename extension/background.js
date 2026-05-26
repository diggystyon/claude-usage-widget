// Claude Usage Bridge - service worker.
//
// Every 60 seconds (and on browser startup), fetch
// /api/organizations/{org}/usage from claude.ai using the user's existing
// session, then POST the JSON to the local widget at 127.0.0.1:38080.
//
// No tab needs to be open. As long as the browser process is alive and the
// user is signed into claude.ai, the bridge keeps running silently.

const WIDGET_ENDPOINT = "http://127.0.0.1:38080/usage";
const BASE_POLL_MINUTES = 1;
const MAX_BACKOFF_MINUTES = 15;
// Track consecutive POST failures to the widget so we can back off when
// the user clearly doesn't have it running. This keeps the extension from
// hammering localhost (and burning a tiny bit of battery) forever when
// the widget isn't installed on this machine. Reset to 0 on success.
let postFailureCount = 0;
let currentPollMinutes = BASE_POLL_MINUTES;

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

async function evictOrgId() {
  try { await chrome.storage.local.remove("orgId"); } catch (_) { /* ignore */ }
}

function setBadge(text, color) {
  try {
    if (color) chrome.action.setBadgeBackgroundColor({ color });
    chrome.action.setBadgeText({ text: text || "" });
  } catch (_) { /* ignore - some browsers may not support */ }
}

function adjustPollCadence() {
  // Exponential back-off: 1 -> 2 -> 4 -> 8 -> 15 minutes. Reset to 1 the
  // moment a POST succeeds. We only touch chrome.alarms when the target
  // actually changes so we don't churn the alarm registration every tick.
  let target;
  if (postFailureCount === 0) {
    target = BASE_POLL_MINUTES;
  } else {
    const exp = Math.min(postFailureCount, 4);  // cap exponent to avoid overflow
    target = Math.min(BASE_POLL_MINUTES * (1 << exp), MAX_BACKOFF_MINUTES);
  }
  if (target === currentPollMinutes) return;
  currentPollMinutes = target;
  chrome.alarms.clear("poll", () => {
    chrome.alarms.create("poll", { periodInMinutes: target, delayInMinutes: target });
  });
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
      // 4xx likely means the cached org changed, was deleted, or the
      // session is no longer valid for it. Evict the cache so the next
      // call re-discovers via /api/organizations. (5xx = transient; keep
      // the cache and let the next poll retry.)
      if (r.status >= 400 && r.status < 500) {
        await evictOrgId();
      }
      setBadge("!", "#cc4444");
      return;
    }
    usage = await r.json();
  } catch (e) {
    setBadge("!", "#cc4444");
    return;
  }
  try {
    const r = await fetch(WIDGET_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(usage),
    });
    if (r.status === 204) {
      // Expected success path. Reset the failure counter and the badge.
      setBadge("");
      postFailureCount = 0;
    } else {
      // Widget IS running on this port (we got a response) but didn't
      // accept the payload. Most likely a version mismatch with a newer
      // widget, or -- worth knowing -- another local process has bound
      // 38080 and is responding to us. Surface this visibly so the user
      // notices their bridge data isn't reaching the real widget.
      console.warn("Claude Usage Bridge: widget returned " + r.status);
      setBadge("!", "#cc4444");
      postFailureCount += 1;
    }
  } catch (e) {
    // Network error: widget not running on this machine. This is the
    // normal case for users without the widget installed. Stay silent
    // (don't badge "!") but count the failure so we can back off.
    setBadge("");
    postFailureCount += 1;
  }
  adjustPollCadence();
}

function ensureAlarm() {
  chrome.alarms.get("poll", (a) => {
    if (!a) {
      chrome.alarms.create("poll", {
        periodInMinutes: currentPollMinutes,
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
