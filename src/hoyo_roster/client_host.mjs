// Browser ownership stays in the AI client's JavaScript runtime.
import { randomBytes } from "node:crypto";
import { spawn } from "node:child_process";
import { isAbsolute } from "node:path";

const LOGIN_URL = "https://act.mihoyo.com/ys/event/calculator/index.html#/character";
const SOURCES = Object.freeze({
  genshin: new Set(["binding", "cultivation-avatar-list"]),
  zzz: new Set(["binding", "record:avatar/basic", "record:avatar/info", "record:buddy/info",
    "cultivate:user/avatar_basic_list", "cultivate:user/batch_avatar_detail_v2", "cultivate:badge-login"]),
});
const BROWSER_TASKS = new WeakSet();
const MAX_FRAME = 16 * 1024 * 1024;
const FAILURES = new Set(["AUTHENTICATION_REQUIRED", "LOGIN_CANCELLED", "LOGIN_TIMEOUT",
  "CLIENT_BROWSER_REQUIRED", "CLIENT_BROWSER_UNAVAILABLE", "CLIENT_SOURCE_UNAVAILABLE",
  "SOURCE_ADAPTER_MISSING",
  "SOURCE_DATA_INVALID", "SOURCE_REQUEST_NOT_OBSERVED",
  "CLIENT_PAGE_UNAVAILABLE", "CLIENT_PROTOCOL_ERROR", "CLIENT_REQUEST_FAILED",
  "RATE_LIMITED", "RISK_CONTROL", "WRONG_ACCOUNT"]);

function failure(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

export function createClientHost(browser, { readSource = null, sourceNames = null,
  checkSources = null, prepareService = null, serviceSources = {} } = {}) {
  if (![browser?.tabs?.list, browser?.tabs?.get, browser?.tabs?.new].every(v => typeof v === "function")) {
    throw failure("CLIENT_BROWSER_REQUIRED");
  }
  if (readSource !== null && typeof readSource !== "function") {
    throw failure("SOURCE_ADAPTER_MISSING");
  }
  if ([checkSources, prepareService].some(fn => fn !== null && typeof fn !== "function")) {
    throw failure("CLIENT_PROTOCOL_ERROR");
  }
  // Copy capability declarations so a later caller mutation cannot widen access.
  const supported = Object.fromEntries(Object.entries(SOURCES).map(([game, names]) => [game, new Set(
    readSource === null ? [] : sourceNames === null ? names : [],
  )]));
  if (sourceNames !== null) {
    if (readSource === null || typeof sourceNames !== "object" || Array.isArray(sourceNames)) {
      throw failure("CLIENT_PROTOCOL_ERROR");
    }
    for (const [game, names] of Object.entries(sourceNames)) {
      if (!Object.hasOwn(SOURCES, game) || !Array.isArray(names) || new Set(names).size !== names.length ||
          names.some(name => !SOURCES[game].has(name))) throw failure("CLIENT_PROTOCOL_ERROR");
      supported[game] = new Set(names);
    }
  }
  if (!serviceSources || typeof serviceSources !== "object" || Array.isArray(serviceSources)) {
    throw failure("CLIENT_PROTOCOL_ERROR");
  }
  const services = new Map(), serviceOnly = new Set();
  for (const [game, declarations] of Object.entries(serviceSources)) {
    if (!Object.hasOwn(SOURCES,game) || !declarations || typeof declarations !== "object" || Array.isArray(declarations)) {
      throw failure("CLIENT_PROTOCOL_ERROR");
    }
    for (const [service, source] of Object.entries(declarations)) {
      if (!/^[a-z][a-z0-9-]*$/.test(service) || !supported[game].has(source)) throw failure("CLIENT_PROTOCOL_ERROR");
      services.set(game+"\0"+service,source);
      serviceOnly.add(game+"\0"+source);
    }
  }
  const session = randomBytes(16).toString("hex");
  let generation = 0;
  const lifetime = new AbortController();
  let loginPage = null;
  let attached = false;
  let pending = null;
  let sequence = 0;
  let active = false;
  let started = false;
  let questionReady = null;
  let stopTask = null;

  function abort(code) {
    if (!lifetime.signal.aborted) lifetime.abort(failure(code));
    if (pending) {
      const current = pending;
      pending = null;
      current.reject(lifetime.signal.reason);
    }
    loginPage = null;
  }

  // Cancellation settles callers even if an SDK call or source adapter never settles.
  // Late results are consumed by the race and cannot reach the worker.
  async function invoke(action) {
    lifetime.signal.throwIfAborted();
    let onAbort;
    const cancelled = new Promise((resolve, reject) => {
      onAbort = () => reject(lifetime.signal.reason);
      lifetime.signal.addEventListener("abort", onAbort, { once: true });
    });
    try {
      return await Promise.race([Promise.resolve().then(() => {
        lifetime.signal.throwIfAborted();
        return action();
      }), cancelled]);
    } finally { lifetime.signal.removeEventListener("abort", onAbort); }
  }

  async function retainPage(page) {
    let tabs;
    try { tabs = await invoke(() => browser.tabs.list()); }
    catch (error) {
      if (lifetime.signal.aborted) throw error;
      throw failure("CLIENT_PAGE_UNAVAILABLE");
    }
    if (!tabs.some(t => t.id === page.id)) throw failure("CLIENT_PAGE_UNAVAILABLE");
    try { await invoke(() => page.markHandoff()); }
    catch (error) {
      if (lifetime.signal.aborted) throw error;
      throw failure("CLIENT_PAGE_UNAVAILABLE");
    }
  }

  async function checkPendingPage() {
    if (!pending) return;
    const current = pending;
    try { await retainPage(current.page); }
    catch (error) {
      if (pending === current) pending = null;
      loginPage = null;
      current.reject(error);
    }
  }

  async function dispatch(method, params) {
    if (method === "session.attach") {
      if (attached || Object.keys(params).length) throw failure("CLIENT_PROTOCOL_ERROR");
      attached = true;
      return { browser: "client-built-in", source_reader: readSource !== null };
    }
    if (!attached) throw failure("CLIENT_PROTOCOL_ERROR");
    if (method === "source.check") {
      if (Object.keys(params).sort().join(",") !== "game,sources" || !Object.hasOwn(SOURCES, params.game) ||
          !Array.isArray(params.sources) || !params.sources.length ||
          new Set(params.sources).size !== params.sources.length ||
          params.sources.some(name => !SOURCES[params.game].has(name))) {
        throw failure("CLIENT_PROTOCOL_ERROR");
      }
      if (readSource === null) throw failure("SOURCE_ADAPTER_MISSING");
      let available = [...supported[params.game]];
      if (checkSources !== null) {
        available = await invoke(() => checkSources({ browser, signal: lifetime.signal,
          game: params.game, sources: params.sources, generation }));
        if (!Array.isArray(available) || new Set(available).size !== available.length ||
            available.some(name => !supported[params.game].has(name))) throw failure("CLIENT_PROTOCOL_ERROR");
      }
      return { available: params.sources.filter(name => available.includes(name) &&
        (!serviceOnly.has(params.game+"\0"+name) || prepareService !== null)) };
    }
    if (method === "service.prepare") {
      const source = services.get(params.game+"\0"+params.service);
      if (!source ||
          Object.keys(params).sort().join(",") !== "game,params,service") throw failure("CLIENT_PROTOCOL_ERROR");
      if (prepareService === null) throw failure("SOURCE_ADAPTER_MISSING");
      if (!supported[params.game].has(source)) throw failure("CLIENT_SOURCE_UNAVAILABLE");
      const result = await invoke(() => prepareService({ browser, signal: lifetime.signal,
        game: params.game, service: params.service, params: params.params, generation }));
      if (!result || result.ready !== true || !Object.hasOwn(result,"origin") ||
          Object.keys(result).sort().join(",") !== "origin,ready") throw failure("CLIENT_PROTOCOL_ERROR");
      return result;
    }
    if (method === "login.open") {
      if (params.url !== LOGIN_URL || Object.keys(params).length !== 1 || loginPage) {
        throw failure("CLIENT_PROTOCOL_ERROR");
      }
      const tabs = await invoke(() => browser.tabs.list());
      const found = tabs.find(t => {
        try {
          const u = new URL(t.url);
          return u.origin === "https://act.mihoyo.com" &&
            u.pathname === "/ys/event/calculator/index.html";
        } catch { return false; }
      });
      const page = await invoke(() => found ? browser.tabs.get(found.id) : browser.tabs.new());
      if (!found) await invoke(() => page.goto(LOGIN_URL));
      await invoke(() => page.markHandoff());
      const capabilities = await invoke(() => browser.capabilities?.list?.() ?? []);
      if (capabilities.some(c => c.id === "visibility")) {
        const visibility = await invoke(() => browser.capabilities.get("visibility"));
        await invoke(() => visibility.set(true));
      }
      loginPage = { page, attempt: randomBytes(16).toString("hex") };
      return { page: loginPage.attempt };
    }
    if (method === "login.confirm") {
      if (!loginPage || loginPage.attempt !== params.page || pending ||
          typeof params.question !== "string" || Object.keys(params).sort().join(",") !== "page,question") {
        throw failure("CLIENT_PROTOCOL_ERROR");
      }
      await retainPage(loginPage.page);
      lifetime.signal.throwIfAborted();
      return await new Promise((resolve, reject) => {
        pending = { ...loginPage, question: params.question, resolve, reject };
        questionReady?.();
      });
    }
    if (method === "source.read") {
      if (!Object.hasOwn(SOURCES, params.game) || !SOURCES[params.game].has(params.source)) {
        throw failure("CLIENT_PROTOCOL_ERROR");
      }
      if (serviceOnly.has(params.game+"\0"+params.source)) throw failure("CLIENT_PROTOCOL_ERROR");
      if (readSource === null) throw failure("SOURCE_ADAPTER_MISSING");
      if (!supported[params.game].has(params.source)) throw failure("CLIENT_SOURCE_UNAVAILABLE");
      const result = await invoke(() => readSource({ browser, signal: lifetime.signal,
        game: params.game, source: params.source, params: params.params, body: params.body, generation }));
      if (!result || typeof result.json !== "string" ||
          !Object.hasOwn(result, "evidence") || !Object.hasOwn(result, "origin") || Object.keys(result).length !== 3) {
        throw failure("CLIENT_PROTOCOL_ERROR");
      }
      return result;
    }
    throw failure("CLIENT_PROTOCOL_ERROR");
  }

  const host = {
    async exchange(message) {
      const valid = message && message.version === 1 && Number.isSafeInteger(message.id) &&
        message.id === sequence + 1 && typeof message.method === "string" &&
        message.params && typeof message.params === "object" && !Array.isArray(message.params) &&
        Object.keys(message).sort().join(",") === "id,method,params,session,version" &&
        (message.method === "session.attach" ? message.session === null : message.session === session);
      if (!valid || active || lifetime.signal.aborted) {
        return { version: 1, id: message?.id ?? 0, session, result: null, error: "CLIENT_PROTOCOL_ERROR" };
      }
      active = true;
      sequence = message.id;
      try {
        const result = await dispatch(message.method, message.params);
        return { version: 1, id: message.id, session, result, error: null };
      } catch (error) {
        return { version: 1, id: message.id, session, result: null,
          error: FAILURES.has(error?.code) ? error.code : "CLIENT_REQUEST_FAILED" };
      } finally { active = false; }
    },

    pendingQuestion() {
      return pending ? { attempt: pending.attempt, question: pending.question,
        options: ["已完成登录", "尚未完成", "取消"] } : null;
    },

    async answerLogin(answer, attempt) {
      if (!pending || pending.attempt !== attempt || !["completed", "waiting", "cancel"].includes(answer)) {
        throw failure("CLIENT_PROTOCOL_ERROR");
      }
      const current = pending;
      if (answer === "cancel") {
        pending = null;
        loginPage = null;
        current.resolve(answer);
        return;
      }
      await checkPendingPage();
      if (pending !== current) return;
      pending = null;
      if (answer !== "waiting") loginPage = null;
      if (answer === "completed") generation++;
      current.resolve(answer);
    },

    cancel() {
      if (stopTask) stopTask("LOGIN_CANCELLED");
      else abort("LOGIN_CANCELLED");
    },

    startTask(options = {}) {
      if (started || attached || active || lifetime.signal.aborted) throw failure("CLIENT_PROTOCOL_ERROR");
      let python, cwd, game, operation, args;
      try {
        if (!options || typeof options !== "object" || Array.isArray(options) ||
            Object.keys(options).some(k => !["python", "cwd", "game", "operation", "storageRoot", "runtimeRoot", "uid"].includes(k))) {
          throw failure("CLIENT_PROTOCOL_ERROR");
        }
        ({ python, cwd, game = "all", operation = "sync" } = options);
        const validString = v => typeof v === "string" && v.length > 0 && !v.includes("\0");
        if (!validString(python) || !validString(cwd) || !isAbsolute(cwd) ||
            !["all", "genshin", "zzz", "hsr"].includes(game) || !["sync", "discover", "login"].includes(operation)) {
          throw failure("CLIENT_PROTOCOL_ERROR");
        }
        args = ["-m", "hoyo_roster", "--client-stdio", "--game", game, "--operation", operation];
        for (const [option, value] of [
          ["--storage-root", options.storageRoot], ["--runtime-root", options.runtimeRoot], ["--uid", options.uid],
        ]) {
          if (value !== undefined) {
            if (!validString(value) || (option !== "--uid" && !isAbsolute(value))) throw failure("CLIENT_PROTOCOL_ERROR");
            args.push(option, value);
          }
        }
      } catch { throw failure("CLIENT_PROTOCOL_ERROR"); }
      if (BROWSER_TASKS.has(browser)) throw failure("USER_WORKSPACE_BUSY");
      BROWSER_TASKS.add(browser);
      let child;
      try { child = spawn(python, args, { cwd, stdio: ["pipe", "pipe", "pipe"] }); }
      catch {
        BROWSER_TASKS.delete(browser);
        throw failure("CLIENT_REQUEST_FAILED");
      }
      started = true;
      const events = [];
      const requests = [];
      let exitCode = null;
      let errorCode = null;
      let endedCode = null;
      let streamEnded = false;
      let incoming = Buffer.alloc(0);
      let inFlight = null;
      let pumping = false;
      let killTimer = null;
      function stop(code) {
        if (exitCode !== null || errorCode) return;
        errorCode = code;
        events.push("ERROR_CODE=" + code);
        abort(code);
        requests.length = 0;
        incoming = Buffer.alloc(0);
        child.stdin.destroy();
        child.kill();
        killTimer = setTimeout(() => child.kill("SIGKILL"), 1000);
        killTimer.unref();
      }
      stopTask = stop;
      child.stderr.on("data", () => {}); // Never expose raw errors or request material.
      child.on("error", () => stop("CLIENT_REQUEST_FAILED"));
      function onLine(line) {
        if (streamEnded) { stop("CLIENT_PROTOCOL_ERROR"); return; }
        let message;
        try { message = JSON.parse(line); } catch { stop("CLIENT_PROTOCOL_ERROR"); return; }
        if (message?.event === "report" && Object.keys(message).sort().join(",") === "event,line" && typeof message.line === "string") {
          if (events.length < 2048) events.push(message.line);
        } else if (message?.event === "end" && Object.keys(message).sort().join(",") === "code,event" &&
                   Number.isInteger(message.code) && message.code >= 0 && message.code <= 255 && !requests.length && !active) {
          endedCode = message.code;
          streamEnded = true;
          child.stdin.end();
        } else {
          if (message?.event || requests.length || active) { stop("CLIENT_PROTOCOL_ERROR"); return; }
          requests.push(message);
        }
      }
      child.stdout.on("data", chunk => {
        if (errorCode || exitCode !== null) return;
        // Bound incomplete frames too, before readline could accumulate unbounded data.
        let offset = 0;
        while (offset < chunk.length && !errorCode) {
          const end = chunk.indexOf(10, offset);
          const limit = end === -1 ? chunk.length : end + 1;
          const part = chunk.subarray(offset, limit);
          if (incoming.length + part.length > MAX_FRAME) { stop("CLIENT_PROTOCOL_ERROR"); break; }
          incoming = incoming.length ? Buffer.concat([incoming, part]) : part;
          if (end !== -1) {
            const raw = incoming;
            incoming = Buffer.alloc(0);
            let line;
            try { line = new TextDecoder("utf-8", { fatal: true }).decode(raw); }
            catch { stop("CLIENT_PROTOCOL_ERROR"); break; }
            onLine(line);
          }
          offset = limit;
        }
      });
      child.stdin.on("error", () => stop("CLIENT_PROTOCOL_ERROR"));
      const completion = new Promise(resolve => child.on("close", code => {
        clearTimeout(killTimer);
        if (!errorCode && (incoming.length || endedCode === null || endedCode !== code)) {
          errorCode = "CLIENT_PROTOCOL_ERROR";
          events.push("ERROR_CODE=" + errorCode);
        }
        // Process lifetime owns the lease. Never wait for an arbitrary source callback here.
        abort(errorCode ?? "CLIENT_BROWSER_UNAVAILABLE");
        requests.length = 0;
        exitCode = errorCode ? 1 : code;
        BROWSER_TASKS.delete(browser);
        resolve({ code: exitCode, errorCode, events: [...events] });
      }));
      const status = () => ({ done: exitCode !== null, code: exitCode, errorCode,
        question: host.pendingQuestion(), pendingRequests: requests.length, events: [...events] });
      return {
        status,
        async pump({ waitMs = 250, maxRequests = 16 } = {}) {
          if (pumping || !Number.isInteger(waitMs) || waitMs < 0 || waitMs > 1000 ||
              !Number.isInteger(maxRequests) || maxRequests < 1 || maxRequests > 64) {
            throw failure("CLIENT_PROTOCOL_ERROR");
          }
          pumping = true;
          try {
            // SDK liveness checks only run inside this explicit active tool call.
            await checkPendingPage();
            for (let handled = 0; handled < maxRequests; handled++) {
              if (pending || exitCode !== null || lifetime.signal.aborted) break;
              if (inFlight) await inFlight;
              if (!requests.length) {
                if (waitMs) await new Promise(r => setTimeout(r, waitMs));
                if (!requests.length || exitCode !== null || lifetime.signal.aborted) break;
              }
              const message = requests.shift();
              const ready = new Promise(resolve => { questionReady = resolve; });
              inFlight = (async () => {
                const reply = await host.exchange(message);
                if (lifetime.signal.aborted) return;
                const encoded = JSON.stringify(reply) + "\n";
                if (Buffer.byteLength(encoded, "utf8") > MAX_FRAME) { stop("CLIENT_PROTOCOL_ERROR"); return; }
                if (child.stdin.writable) child.stdin.write(encoded);
              })().catch(() => stop("CLIENT_PROTOCOL_ERROR"));
              const current = inFlight;
              await Promise.race([current, ready]);
              questionReady = null;
              if (pending) break;
              await current;
              if (inFlight === current) inFlight = null;
            }
            // Yield to process-close I/O after cancellation; callers may keep pumping.
            if (lifetime.signal.aborted && exitCode === null) await completion;
            return status();
          } finally { pumping = false; }
        },
        completion,
        cancel: () => stop("LOGIN_CANCELLED"),
      };
    },
  };
  return host;
}
