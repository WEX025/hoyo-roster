import test from "node:test";
import assert from "node:assert/strict";
import { runInNewContext } from "node:vm";
import { createGenshinPageSource } from "../src/hoyo_roster/games/genshin/page_source.mjs";
import { createMetrics } from "../src/hoyo_roster/client_metrics.mjs";

function fixture() {
  const selected = new Set(), names = ["Alpha", "Beta", "Gamma", "Delta"];
  const counts = { reload: 0, back: 0 };
  let edit = false, nickname = "Synthetic", current, blocker = "none";
  const role = { uid: "123456789", nickname: "Synthetic", region: "Synthetic region" };
  const inputs = value => [{ value: String(value) }, { value: "90" }];
  const cards = names.map(name => ({
    querySelector(selector) {
      if (selector === ".gt-avatar-selected") return selected.has(name) ? {} : null;
      return { textContent: selector === ".gt-avatar-name p" ? "Lv.50" : name };
    },
  }));
  const containers = () => [...selected].map(name => ({
    querySelector(selector) {
      return { innerText: selector === ".avatar-section" ? name : "Synthetic weapon",
        textContent: "已同步拥有角色的信息", querySelectorAll: () => inputs(50) };
    },
    querySelectorAll: () => ["普通攻击", "元素战技", "元素爆发"].map(category => ({
      innerText: category + "\nSynthetic skill", querySelectorAll: () => inputs(1),
    })),
  }));
  const document = { querySelector: () => ({}), querySelectorAll: selector => selector === ".character-item" ? cards : containers() };
  const target = name => ({ text: name, evaluate: async () => ({hover:blocker === "hover",blocked:blocker !== "none"}), isVisible: async () => !edit,
    evaluate: async () => true,
    click: async () => { if (name === "选择角色") { blocker = "none"; return; } if (selected.has(name)) selected.delete(name); else selected.add(name); } });
  const tab = { id: "synthetic", reload: async () => { counts.reload++; edit = false; selected.clear(); },
    back: async () => { counts.back++; edit = false; },
    url: async () => "https://act.mihoyo.com/ys/event/calculator/index.html#/character" + (edit ? "/edit" : ""),
    playwright: {
      evaluate: async fn => runInNewContext("(" + fn.toString() + ")()", { document }),
      domSnapshot: async () => "Synthetic state",
      getByText: target,
      getByRole: () => ({ click: async () => { edit = true; } }),
      locator: selector => ({
        count: async () => 1,
        innerText: async () => nickname,
        filter: ({ has }) => ({ click: target(has.text).click, getByText: target }),
      }),
    } };
  current = tab;
  const ui = { page: async () => current, identity: async () => ({ ...role }),
    assertIdentity: (a, b) => { if (a.uid !== b.uid || a.region !== b.region) throw Object.assign(new Error(), { code: "WRONG_ACCOUNT" }); },
    call: async (signal, action) => { signal.throwIfAborted(); const result = await action(); signal.throwIfAborted(); return result; },
    measure: async (_name, signal, action) => ui.call(signal, action),
    wait: async (signal, read, accept) => { const value = await ui.call(signal, read); assert.ok(accept(value)); return value; },
    click: async (_tab, locator, signal) => { await ui.call(signal, locator.click); return "Synthetic state"; },
    record: (_game, _url, _surface, data) => data,
  };
  const reader = createGenshinPageSource(ui);
  const query = (source, extra = {}, generation = 0) => ({ source, params: { uid: role.uid, region: role.region, ...extra }, body: null, generation, signal: new AbortController().signal });
  return { counts, reader, query, selected, setBlocker: value => { blocker = value; }, setNickname: n => { nickname = n; }, replacePage: () => { current = { ...tab, id: "replacement" }; } };
}

test("Genshin reuses only the current round, reconciles selected cards and returns the requested details", async () => {
  const f = fixture();
  const roster = await f.reader.read(f.query("page:roster"));
  assert.equal(roster.rows.length, 4);
  const first = await f.reader.read(f.query("page:detail", { names: ["Alpha", "Beta"] }));
  assert.deepEqual(Array.from(first.details, d => d.name), ["Alpha", "Beta"]);
  const second = await f.reader.read(f.query("page:detail", { names: ["Gamma", "Delta"] }));
  assert.deepEqual(Array.from(second.details, d => d.name), ["Gamma", "Delta"]);
  assert.equal(f.counts.reload, 1);
  assert.equal(f.counts.back, 1);
  await f.reader.read(f.query("page:roster"));
  assert.equal(f.counts.reload, 2); // A second pass must prepare fresh observations.
});

test("Genshin refuses stale login generations, replaced pages and an observed changed nickname", async () => {
  for (const scenario of ["generation", "replacement", "identity"]) {
    const f = fixture();
    await f.reader.read(f.query("page:roster"));
    if (scenario === "replacement") f.replacePage();
    if (scenario === "identity") f.setNickname("Other synthetic person");
    await assert.rejects(f.reader.read(f.query("page:detail", { names: ["Alpha"] }, scenario === "generation" ? 1 : 0)),
      { code: scenario === "identity" ? "WRONG_ACCOUNT" : "SOURCE_DATA_INVALID" });
    f.setNickname("Synthetic");
    await assert.rejects(f.reader.read(f.query("page:detail", { names: ["Alpha"] })), { code: "SOURCE_DATA_INVALID" });
  }
});

test("cancelling a cached round prevents any later navigation or returned details", async () => {
  const f = fixture();
  await f.reader.read(f.query("page:roster"));
  await f.reader.read(f.query("page:detail", { names: ["Alpha"] }));
  const controller = new AbortController(); controller.abort(new Error("Synthetic cancellation"));
  await assert.rejects(f.reader.read({ ...f.query("page:detail", { names: ["Beta"] }), signal: controller.signal }), /Synthetic cancellation/);
  assert.equal(f.counts.back, 0);
});

test("metrics keep unique fixed metadata, record failures and drain without retaining results", async () => {
  const metrics = createMetrics();
  assert.deepEqual(await metrics.measure("source.read", async () => ({ private: "synthetic" }), { game: "genshin" }), { private: "synthetic" });
  await assert.rejects(metrics.measure("source.read", async () => { throw new Error("synthetic private error"); }));
  const events = metrics.drain();
  assert.deepEqual(events.map(e => e.id), [1, 2]);
  assert.deepEqual(events.map(e => e.status), ["PASS", "FAIL"]);
  assert.ok(events.every(e => e.duration_ms >= 0));
  assert.equal(JSON.stringify(events).includes("synthetic"), false);
  assert.deepEqual(metrics.drain(), []);
});

test("active runner pumps only single requests and hands back login questions immediately", async () => {
  const { pumpWindow } = await import("../src/hoyo_roster/client_runner.mjs");
  let pumps = 0, records = 0;
  const task = { status: () => ({ done: false, question: null }), pump: async options => {
    assert.equal(options.maxRequests, 1); pumps++;
    return { done: false, question: { attempt: 1 } };
  } };
  assert.equal((await pumpWindow(task, { onStep: () => { records++; } })).question.attempt, 1);
  assert.equal(pumps, 1); assert.equal(records, 1);
  await pumpWindow(task, { budgetMs: 0 });
  assert.equal(pumps, 1);
});

test("ZZZ reuses a round roster but reads fresh details for each selected owned card", async () => {
  const { createZzzPageSource } = await import("../src/hoyo_roster/games/zzz/page_source.mjs");
  const role = { uid: "123456789", region: "Synthetic", nickname: "Synthetic" };
  let opened = false, currentName = "Initial", scans = 0;
  const rect = () => ({ width: 100, height: 100 });
  const cardFor = name => ({ querySelector: () => ({ textContent: name }), innerText: "Lv.50 " + name });
  const marker = text => ({ textContent: text, children: [], getBoundingClientRect: rect,
    parentElement: { innerText: text + " synthetic " + currentName, parentElement: { innerText: text + " synthetic " + currentName } },
    closest: () => ({ innerText: text + " synthetic " + currentName }) });
  const document = { body: { innerText: "Synthetic" }, querySelectorAll(selector) {
    if (selector === ".agent-list__row .gt-card") { scans++; return [cardFor("Alpha"), cardFor("Beta")]; }
    if (selector === "div.relative.z-2") return [{ textContent: currentName, parentElement: { classList: { contains: () => true } } }];
    if (selector === "p,div") return ["当前音擎", "当前技能等级", "当前装备驱动盘"].map(marker);
    if (selector === ".agent-list__row") return opened ? [{ getBoundingClientRect: rect }] : [];
    return [];
  } };
  const target = name => ({ isVisible: async () => name === "选择代理人" ? opened : true,
    click: async () => { currentName = name; opened = false; } });
  const tab = { id: "synthetic", reload: async () => {}, url: async () => "https://act.mihoyo.com/zzz/gt/character-builder-h/index.html#/",
    playwright: { evaluate: async fn => runInNewContext("(" + fn.toString() + ")()", { document }),
      getByRole: () => ({ isVisible: async () => false }), getByText: target,
      locator: () => ({ click: async () => { opened = true; }, filter: ({ has }) => ({
        isVisible: async () => opened, innerText: async () => "Lv.50 " + has.text,
        getByText: target,
      }) }),
    } };
  // The card locator's text is only a synthetic matching label.
  const oldTarget = tab.playwright.getByText;
  tab.playwright.getByText = name => ({ ...oldTarget(name), text: name });
  const ui = { page: async () => tab, identity: async () => role, assertIdentity: () => {},
    call: async (signal, fn) => { signal.throwIfAborted(); return fn(); },
    measure: async (_name, _signal, fn) => fn(),
    click: async (_tab, locator) => { await locator.click(); return "当前技能等级"; },
    wait: async (_signal, read, accept) => { const value = await read(); assert.ok(accept(value)); return value; },
    record: (_game, _url, _surface, value) => value,
  };
  const source = createZzzPageSource(ui);
  const query = (kind, names) => ({ source: "page:" + kind, body: null, generation: 0, signal: new AbortController().signal,
    params: { uid: role.uid, region: role.region, ...(names ? { names } : {}) } });
  await source.read(query("roster"));
  const first = await source.read(query("detail", ["Alpha"]));
  const second = await source.read(query("detail", ["Beta"]));
  assert.equal(first.details[0].name, "Alpha"); assert.equal(first.details[0].display_name, "Alpha");
  assert.equal(second.details[0].name, "Beta"); assert.equal(second.details[0].display_name, "Beta");
  assert.ok(second.details[0].current_skills.includes("Beta"));
  assert.equal(scans, 1);
});


test("Genshin dismisses an observed character hover but refuses an unknown overlay", async () => {
  for (const kind of ["hover", "unknown"]) {
    const f = fixture();
    await f.reader.read(f.query("page:roster"));
    f.setBlocker(kind);
    const read = f.reader.read(f.query("page:detail", { names: ["Alpha"] }));
    if (kind === "hover") assert.equal((await read).details[0].name, "Alpha");
    else { await assert.rejects(read, { code: "SOURCE_DATA_INVALID" }); assert.equal(f.selected.size, 0); }
  }
});
