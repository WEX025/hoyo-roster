// Only the selected client's advertised CDP capability is used. No browser launch,
// debugging address discovery, cookie access, external HTTP client or SDK internals.
const METHODS = ["Network.requestWillBeSent", "Network.responseReceived",
  "Network.loadingFinished", "Network.loadingFailed"];
const MAX_BODY = 12 * 1024 * 1024;

export function sourceFailure(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

export function checkSignal(signal) { signal.throwIfAborted(); }

export async function sourceStep(query, action, timeoutMs = 45000) {
  checkSignal(query.signal);
  const deadline = new AbortController();
  const timer = setTimeout(() => deadline.abort(sourceFailure("SOURCE_REQUEST_NOT_OBSERVED")), timeoutMs);
  const signal = AbortSignal.any([query.signal, deadline.signal]);
  let onAbort;
  const cancelled = new Promise((resolve, reject) => {
    onAbort = () => reject(signal.reason);
    signal.addEventListener("abort", onAbort, { once: true });
  });
  try {
    return await Promise.race([Promise.resolve().then(() => {
      checkSignal(signal);
      return action({ ...query, signal });
    }), cancelled]);
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", onAbort);
  }
}

function receiptJson(text) {
  let result;
  try {
    result = JSON.parse(text);
    let index = 0;
    const space = () => { while (/\s/.test(text[index] ?? "") && index < text.length) index++; };
    function string() {
      const start = index++;
      while (index < text.length) {
        const char = text[index++];
        if (char === "\\") index++;
        else if (char === '"') return JSON.parse(text.slice(start, index));
      }
      throw new Error();
    }
    function value(depth = 0) {
      if (depth > 64) throw new Error();
      space();
      if (text[index] === "{") {
        index++; space();
        const keys = new Set();
        while (text[index] !== "}") {
          const key = string();
          if (keys.has(key)) throw new Error();
          keys.add(key); space(); index++; space();
          const start = index;
          value(depth + 1);
          if (depth === 0 && key === "retcode" && !/^-?(?:0|[1-9][0-9]*)$/.test(text.slice(start,index))) throw new Error();
          space();
          if (text[index] !== ",") break;
          index++; space();
        }
        index++;
      } else if (text[index] === "[") {
        index++; space();
        while (text[index] !== "]") {
          value(depth + 1); space();
          if (text[index] !== ",") break;
          index++;
        }
        index++;
      } else if (text[index] === '"') string();
      else while (index < text.length && !/[\s,\]}]/.test(text[index])) index++;
    }
    value();
    return result;
  } catch { throw sourceFailure("SOURCE_DATA_INVALID"); }
}

function sameEndpoint(url, expected) {
  try {
    const actual = new URL(url), target = new URL(expected);
    return actual.origin === target.origin && actual.pathname === target.pathname;
  } catch { return false; }
}

export function requireReceipt(record) {
  if (record.origin.redirected) throw sourceFailure("SOURCE_DATA_INVALID");
  if (record.origin.status === 429) throw sourceFailure("RATE_LIMITED");
  if (record.origin.status < 200 || record.origin.status >= 300) throw sourceFailure("SOURCE_DATA_INVALID");
  const payload = receiptJson(record.json);
  if (!payload || !Number.isInteger(payload.retcode)) throw sourceFailure("SOURCE_DATA_INVALID");
  if ([-100, -101, 10001].includes(payload.retcode)) throw sourceFailure("AUTHENTICATION_REQUIRED");
  if ([1034, 10035, 5003, 10041].includes(payload.retcode)) throw sourceFailure("RISK_CONTROL");
  if (payload.retcode === 429) throw sourceFailure("RATE_LIMITED");
  if (payload.retcode !== 0 || !payload.data || typeof payload.data !== "object" || Array.isArray(payload.data)) {
    throw sourceFailure("SOURCE_DATA_INVALID");
  }
  // Only used for service receipts. Business JSON is never parsed/re-encoded here.
}

export function createClientNetwork(browser) {
  const pages = new Map();
  function requirePageOrigin(actual, expected) {
    try {
      if (new URL(actual).origin === new URL(expected).origin) return;
    } catch {}
    throw sourceFailure("SOURCE_DATA_INVALID");
  }
  async function pageFor(url, signal, { probe = false } = {}) {
    checkSignal(signal);
    const tabs = await browser.tabs.list();
    checkSignal(signal);
    const retained = tabs.find(tab => tab.id === pages.get(url));
    if (retained) requirePageOrigin(retained.url, url);
    const found = retained ?? tabs.find(tab => sameEndpoint(tab.url, url));
    const page = await (found ? browser.tabs.get(found.id) : browser.tabs.new());
    checkSignal(signal);
    const capabilities = await page.capabilities?.list?.() ?? [];
    checkSignal(signal);
    if (!capabilities.some(capability => capability.id === "cdp")) {
      if (!found) await page.close(); // This is our new, still-blank probe tab.
      throw sourceFailure("CLIENT_SOURCE_UNAVAILABLE");
    }
    if (probe) {
      if (!found) await page.close();
      return null;
    }
    await page.markHandoff();
    checkSignal(signal);
    // The documented CDP API requires an HTTP(S) page before its first command.
    if (!found) await page.goto(url);
    checkSignal(signal);
    requirePageOrigin(await page.url(), url);
    checkSignal(signal);
    pages.set(url, page.id);
    return page;
  }

  async function available(url, signal) {
    try { await pageFor(url, signal, { probe: true }); return true; }
    catch (error) {
      if (error.code === "CLIENT_SOURCE_UNAVAILABLE") return false;
      throw error;
    }
  }

  async function monitor(page, signal, trigger, consume, { timeoutMs = 30000, settleMs = 0 } = {}) {
    checkSignal(signal);
    const cdp = await page.capabilities.get("cdp");
    checkSignal(signal);
    const send = async (method, params = {}) => {
      checkSignal(signal);
      try {
        const result = await cdp.send(method, params, { timeoutMs: 15000 });
        checkSignal(signal);
        return result;
      } catch (error) {
        checkSignal(signal);
        throw sourceFailure("CLIENT_SOURCE_UNAVAILABLE");
      }
    };
    await send("Network.enable", { maxResourceBufferSize: MAX_BODY, maxTotalBufferSize: MAX_BODY * 2 });
    try {
      let batch = await cdp.readEvents({ methods: METHODS, limit: 1000 });
      if (batch.truncated || !Number.isSafeInteger(batch.cursor)) throw sourceFailure("SOURCE_DATA_INVALID");
      let cursor = batch.cursor;
      await trigger({ send });
      const deadline = Date.now() + timeoutMs;
      let result, settledAt;
      while (Date.now() < deadline) {
        checkSignal(signal);
        batch = await cdp.readEvents({ afterSequence: cursor, methods: METHODS, limit: 1000,
          timeoutMs: Math.min(1000, Math.max(1, deadline - Date.now())) });
        checkSignal(signal);
        if (batch.truncated || !Number.isSafeInteger(batch.cursor) || batch.cursor < cursor ||
            (batch.hasMore && batch.cursor === cursor)) throw sourceFailure("SOURCE_DATA_INVALID");
        for (const event of batch.events) {
          const next = await consume(event, { send });
          if (next !== undefined && result === undefined) {
            result = next;
            settledAt = Date.now() + settleMs;
          }
        }
        cursor = batch.cursor;
        if (result !== undefined && !batch.hasMore && Date.now() >= settledAt) return result;
      }
      throw sourceFailure("SOURCE_REQUEST_NOT_OBSERVED");
    } finally {
      // Do not issue further browser commands after cancellation.
      if (!signal.aborted) await send("Network.disable");
    }
  }

  async function observe(page, spec, signal, trigger, { onRequest, settleMs = 0 } = {}) {
    const requests = new Map(), responses = new Map();
    let result;
    await monitor(page, signal, trigger, async (event, io) => {
      const data = event.params ?? {};
      if (event.method === "Network.requestWillBeSent") {
        if (!sameEndpoint(data.request?.url, spec.url) && !requests.has(data.requestId)) return;
        if (data.redirectResponse) throw sourceFailure("SOURCE_DATA_INVALID");
        const request = data.request;
        if (!request || request.method !== spec.method || !sameEndpoint(request.url, spec.url)) {
          throw sourceFailure("SOURCE_DATA_INVALID");
        }
        requests.set(data.requestId, { url: request.url, method: request.method, postData: request.postData });
        onRequest?.(requests.get(data.requestId));
      } else if (event.method === "Network.responseReceived" && requests.has(data.requestId)) {
        const response = data.response;
        if (!response || !sameEndpoint(response.url, spec.url) || !Number.isInteger(response.status)) {
          throw sourceFailure("SOURCE_DATA_INVALID");
        }
        if (response.fromServiceWorker || response.fromDiskCache) throw sourceFailure("SOURCE_DATA_INVALID");
        responses.set(data.requestId, { url: response.url, method: requests.get(data.requestId).method,
          status: response.status, redirected: false });
      } else if (event.method === "Network.loadingFailed" && requests.has(data.requestId)) {
        throw sourceFailure(data.blockedReason ? "CLIENT_SOURCE_UNAVAILABLE" : "CLIENT_REQUEST_FAILED");
      } else if (event.method === "Network.loadingFinished" && responses.has(data.requestId) && result === undefined) {
        const origin = responses.get(data.requestId);
        if (origin.status === 429) throw sourceFailure("RATE_LIMITED");
        const body = await io.send("Network.getResponseBody", { requestId: data.requestId });
        if (!body || typeof body.body !== "string" || typeof body.base64Encoded !== "boolean") {
          throw sourceFailure("SOURCE_DATA_INVALID");
        }
        const bytes = body.base64Encoded ? Buffer.from(body.body, "base64") : Buffer.from(body.body, "utf8");
        if (bytes.length > MAX_BODY) throw sourceFailure("SOURCE_DATA_INVALID");
        let json;
        try { json = new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
        catch { throw sourceFailure("SOURCE_DATA_INVALID"); }
        result = { json, origin, evidence: null };
        return result;
      }
    }, { settleMs });
    return result;
  }

  async function navigate(page, url, signal) {
    checkSignal(signal);
    if (await page.url() === url) await page.reload();
    else await page.goto(url);
    checkSignal(signal);
    requirePageOrigin(await page.url(), url);
    checkSignal(signal);
  }

  async function request(page, spec, signal, { params = {}, body = null, headers = {} } = {}) {
    const target = new URL(spec.url);
    for (const key of Object.keys(params).sort()) target.searchParams.append(key, String(params[key]));
    const options = { method: spec.method, credentials: "include", redirect: "error", cache: "no-store", headers };
    if (body !== null) options.body = JSON.stringify(body);
    return observe(page, spec, signal, async ({ send }) => {
      // This command is only reachable through an advertised, permitted CDP capability.
      // Browser-managed credentials stay in this same tab/session; no auth material is read.
      const result = await send("Runtime.evaluate", { expression:
        `void fetch(${JSON.stringify(target.href)},${JSON.stringify(options)}).catch(() => undefined)`,
      returnByValue: true, awaitPromise: false });
      if (result?.exceptionDetails) throw sourceFailure("CLIENT_REQUEST_FAILED");
    });
  }

  async function deviceHeaders(page, url, signal) {
    const allowed = new Set(["x-rpc-device_id", "x-rpc-device_fp", "x-rpc-geetest_ext", "x-rpc-page", "x-rpc-platform"]);
    const hosts = new Set(["api-takumi.mihoyo.com", "api-takumi-record.mihoyo.com", "passport-api.mihoyo.com"]);
    const collected = {};
    return monitor(page, signal, () => navigate(page, url, signal), event => {
      if (event.method !== "Network.requestWillBeSent") return;
      const request = event.params?.request;
      let target;
      try { target = new URL(request?.url); } catch { return; }
      if (target.protocol !== "https:" || !hosts.has(target.hostname)) return;
      for (const name of Object.keys(request.headers ?? {})) {
        const lowered = name.toLowerCase();
        if (!allowed.has(lowered)) continue;
        const value = request.headers[name];
        if (typeof value === "string" && value) collected[lowered] = value;
      }
      if (collected["x-rpc-device_id"] && collected["x-rpc-device_fp"]) return collected;
    }, { timeoutMs: 10000 });
  }

  return Object.freeze({ available, pageFor, observe, navigate, request, deviceHeaders });
}
