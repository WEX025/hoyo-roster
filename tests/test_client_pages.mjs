import test from "node:test";
import assert from "node:assert/strict";
import { createPageBrowser } from "../src/hoyo_roster/client_page.mjs";
import { dismissSchemeNotice, createZzzPageSource } from "../src/hoyo_roster/games/zzz/page_source.mjs";

const URL = "https://act.mihoyo.com/ys/event/calculator/index.html#/character";

test("ZZZ retries only an explicit official error once, preserving the same page and auth errors", async () => {
  for (const scenario of ["recover", "still-fails", "unknown", "auth"]) {
    let failed = true, clicks = 0, identities = 0;
    const tab = { reload: async () => {}, url: async () => "https://act.mihoyo.com/zzz/gt/character-builder-h/index.html#/",
      playwright: { getByRole: () => ({ isVisible: async () => false }),
        getByText: text => ({ text, isVisible: async () => failed && text === "出错了，请重试" && scenario !== "unknown" }),
        locator: () => ({ count: async () => 1 }) } };
    const ui = { measure: async (_name, _signal, action) => action(), page: async () => tab, call: async (_signal, action) => action(),
      identity: async selected => { assert.equal(selected, tab); identities++;
        if (failed) throw Object.assign(new Error("synthetic"), { code: scenario === "auth" ? "AUTHENTICATION_REQUIRED" : "SOURCE_REQUEST_NOT_OBSERVED" });
        return { uid: "123456789", nickname: "Synthetic", region: "Synthetic region" };
      }, click: async (selected, target) => { assert.equal(selected, tab); assert.equal(target.text, "重试"); clicks++; if (scenario === "recover") failed = false; },
      wait: async (_signal, read, accept) => assert.ok(accept(await read())), record: (...args) => args };
    const read = createZzzPageSource(ui).read({ source: "page:identity", params: {}, body: null,
      signal: new AbortController().signal, generation: 0 });
    if (scenario === "recover") assert.equal((await read)[3].nickname, "Synthetic");
    else await assert.rejects(read, { code: scenario === "auth" ? "AUTHENTICATION_REQUIRED" : "SOURCE_REQUEST_NOT_OBSERVED" });
    assert.equal(clicks, ["recover", "still-fails"].includes(scenario) ? 1 : 0);
    assert.equal(identities, clicks + 1);
  }
});

test("ZZZ dismisses only the observed informational notice without changing schemes", async () => {
  for (const visible of [false, true]) {
    const calls = [];
    let shown = visible;
    const tab = { playwright: { getByRole: (role, options) => {
      calls.push([role, options]);
      return { role, isVisible: async () => shown };
    } } };
    const ui = { call: async (_signal, action) => action(), click: async (_tab, target) => {
      assert.equal(target.role, "button"); calls.push("acknowledge"); shown = false;
    }, wait: async (_signal, read, accept) => assert.ok(accept(await read())) };
    assert.equal(await dismissSchemeNotice(ui, tab, new AbortController().signal), visible);
    assert.deepEqual(calls, [["heading", { name: "方案更新提示", exact: true }],
      ...(visible ? [["button", { name: "知道了", exact: true }], "acknowledge"] : [])]);
  }
});

test("ZZZ waits for a closing notice instead of clicking a disappearing button", async () => {
  for (const race of [false, true]) {
    let shown = true, button = race, clicks = 0;
    const tab = { playwright: { getByRole: role => ({
      isVisible: async () => role === "heading" ? shown : button,
    }) } };
    const ui = { call: async (_signal, action) => action(),
      click: async () => { clicks++; button = false; shown = false; throw new Error("no_matches"); },
      wait: async (_signal, read, accept) => { shown = false; assert.ok(accept(await read())); } };
    assert.equal(await dismissSchemeNotice(ui, tab, new AbortController().signal), true);
    assert.equal(clicks, race ? 1 : 0);
  }
});
function fixture({ zero = false, retained = true, freshZero = false } = {}) {
  const calls = [];
  const page = { id: "owned", url: async () => URL, markHandoff: async () => calls.push("retain"),
    goto: async () => calls.push("goto"), close: async () => calls.push("close"),
    playwright: { evaluate: async () => ({ width: zero ? 0 : 1280, height: zero ? 0 : 900 }),
      domSnapshot: async () => "safe state", locator: () => ({}), getByText: () => ({}) } };
  Object.defineProperty(page, "cookies", { get() { throw new Error("credential access forbidden"); } });
  const fresh = { ...page, id: "fresh", playwright: { ...page.playwright,
    evaluate: async () => ({ width: freshZero ? 0 : 1280, height: freshZero ? 0 : 900 }) } };
  const browser = { tabs: { list: async () => retained ? [{ id: page.id, url: URL }] : [],
    get: async () => page, new: async () => { calls.push("new"); return fresh; } }, capabilities: {
    list: async () => [{ id: "viewport" }], get: async id => {
      assert.equal(id, "viewport");
      return { set: async () => { zero = false; freshZero = false; calls.push("layout"); }, reset: async () => calls.push("reset") };
    } } };
  return { browser, page, calls, ui: createPageBrowser(browser) };
}

test("UI reader borrows the same browser/tab and never requires CDP or credentials", async () => {
  const f = fixture();
  assert.equal(await f.ui.page(URL, new AbortController().signal), f.page);
  assert.deepEqual(f.calls, ["retain"]);
});

test("unmounted retained tab is replaced within the same browser without touching its credentials", async () => {
  const f = fixture({ zero: true });
  assert.equal((await f.ui.page(URL, new AbortController().signal)).id, "fresh");
  await f.ui.release();
  assert.deepEqual(f.calls, ["new", "goto", "retain"]);
});

test("a fresh zero viewport gets a temporary layout repair and is restored once", async () => {
  const f = fixture({ zero: true, freshZero: true, retained: false });
  await f.ui.page(URL, new AbortController().signal);
  await f.ui.release();
  await f.ui.release();
  assert.deepEqual(f.calls, ["new", "goto", "layout", "retain", "reset"]);
});

test("page record is explicitly UI provenance, without invented HTTP facts", () => {
  const f = fixture();
  const record = f.ui.record("genshin", URL, "owned-selector", { rows: [] }, 3);
  assert.deepEqual(record.origin, { kind: "official-page", game: "genshin", url: URL });
  assert.equal(record.evidence.generation, 3);
  assert.equal(record.json, '{"rows":[]}');
  assert.equal(Object.hasOwn(record.origin, "status"), false);
});

test("stable loading rejects an intermediate roster before accepting two equal reads", async () => {
  const f = fixture();
  const values = [[], ["one"], ["one", "two"], ["one", "two"]];
  let count = 0;
  const result = await f.ui.wait(new AbortController().signal,
    async () => values[count++], r => r.length > 0, { stable: true, timeoutMs: 1500 });
  assert.deepEqual(result, ["one", "two"]);
  assert.equal(count, 4);
});

test("cancellation after a pending SDK call prevents retention and later actions", async () => {
  const f = fixture();
  const controller = new AbortController();
  f.page.playwright.evaluate = async () => { controller.abort(new Error("cancel")); return { width: 1280, height: 900 }; };
  await assert.rejects(f.ui.page(URL, controller.signal), /cancel/);
  assert.deepEqual(f.calls, []);
});

test("a replaced retained tab at an unexpected origin fails before any UI action", async () => {
  const f = fixture();
  await f.ui.page(URL, new AbortController().signal);
  f.browser.tabs.list = async () => [{ id: "owned", url: "https://example.com/" }];
  await assert.rejects(f.ui.page(URL, new AbortController().signal), { code: "SOURCE_DATA_INVALID" });
  assert.deepEqual(f.calls, ["retain"]);
});

test("identity closes an observed still-open panel with one bounded repair", async () => {
  let opened = true, clicks = 0;
  const ui = createPageBrowser({ tabs: {} });
  const tab = { playwright: {
    evaluate: async fn => fn.toString().includes('const fields') ? false :
      [{ uid: "123456789", nickname: "Synthetic", region: "Synthetic" }],
    locator: selector => ({ isVisible: async () => opened, click: async () => {
      assert.equal(selector, '.mhy-game-role-message-box-header-close-icon');
      clicks++; if (clicks === 2) opened = false;
    } }),
  } };
  const role = await ui.identity(tab, new AbortController().signal, async () => assert.fail('already open'));
  assert.equal(role.uid, '123456789'); assert.equal(clicks, 2); assert.equal(opened, false);
});
