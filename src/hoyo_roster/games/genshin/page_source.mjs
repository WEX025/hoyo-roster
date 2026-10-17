import { pageSourceNames } from "../../client_page.mjs";
import { sourceFailure } from "../../client_network.mjs";

export const PAGE = "https://act.mihoyo.com/ys/event/calculator/index.html#/character";
export const sourceNames = pageSourceNames;
const UNSUPPORTED = "完成特定任务可达成奇偶突破、升级条件，养成计算器中暂不支持奇偶相关的计算";

export function createGenshinPageSource(ui) {
  let prepared = null;
  const measure = (name, signal, action) => ui.measure(name, signal, action);
  async function identity(tab, signal) {
    const role = await ui.identity(tab, signal, async () => {
      await ui.wait(signal, async () => {
        if (await ui.authentication(tab, signal)) throw sourceFailure("AUTHENTICATION_REQUIRED");
        return await ui.call(signal, () => tab.playwright.locator(".user-wrapper .switch").count()) === 1 ||
          await ui.call(signal, () => tab.playwright.getByText("已拥有", { exact: true }).isVisible());
      }, Boolean);
      // An anonymous calculator shows the public catalog without an account
      // header. Its owned tab reveals the official login prompt, never a roster.
      if (!(await ui.call(signal, () => tab.playwright.locator(".user-wrapper .switch").count()))) {
        await ui.click(tab, tab.playwright.getByText("已拥有", { exact: true }), signal);
      }
      await ui.authenticatedControl(tab, ".user-wrapper .switch", signal);
      await ui.click(tab, tab.playwright.locator(".user-wrapper .switch"), signal);
    });
    const nickname = await ui.call(signal, () => tab.playwright.locator(".user-wrapper .nickname").innerText({ timeoutMs: 1500 }));
    if (nickname.trim() !== role.nickname) throw sourceFailure("WRONG_ACCOUNT");
    return role;
  }
  async function owned(tab, signal, params) {
    await measure("genshin.reload", signal, () => tab.reload());
    const role = await measure("genshin.identity", signal, () => identity(tab, signal));
    ui.assertIdentity(role, params);
    const hasOwned = await ui.call(signal, () => tab.playwright.getByText("已拥有", { exact: true }).isVisible());
    if (!hasOwned) await ui.click(tab, tab.playwright.getByText("角色", { exact: true }), signal);
    await ui.wait(signal, () => tab.playwright.getByText("已拥有", { exact: true }).isVisible(), Boolean);
    const active = await ui.call(signal, () => tab.playwright.getByText("已拥有", { exact: true })
      .evaluate(e => e.parentElement.classList.contains("active")));
    if (!active) await ui.click(tab, tab.playwright.getByText("已拥有", { exact: true }), signal);
    const rows = await measure("genshin.roster", signal, () => ui.wait(signal, () => tab.playwright.evaluate(() =>
      Array.from(document.querySelectorAll(".character-item")).map(e => ({
        name: e.querySelector("p.gt-mobile-caption-c2-3")?.textContent?.trim(),
        level: e.querySelector(".gt-avatar-name p")?.textContent?.trim(),
      }))), rows => rows.length > 0 && rows.every(r => r.name && /^Lv\.[1-9][0-9]*$/.test(r.level ?? "")), { stable: true }));
    return { rows, role };
  }
  async function read({ source, params, body, signal, generation }) {
    if (body !== null || !params || typeof params !== "object" || Array.isArray(params)) throw sourceFailure("CLIENT_PROTOCOL_ERROR");
    const tab = await ui.page(PAGE, signal);
    if (source === "page:identity") {
      prepared = null;
      if (Object.keys(params).length) throw sourceFailure("CLIENT_PROTOCOL_ERROR");
      // Another game tab may have completed shared login while this SPA still
      // renders its anonymous catalog. Re-read the official page in this session.
      await measure("genshin.reload", signal, () => tab.reload());
      const role = await measure("genshin.identity", signal, () => identity(tab, signal));
      return ui.record("genshin", await ui.call(signal, () => tab.url()), "role-selector", role, generation);
    }
    const fields = source === "page:detail" ? "names,region,uid" : "region,uid";
    if (!sourceNames.includes(source) || Object.keys(params).sort().join(",") !== fields ||
      !/^[1-9][0-9]{0,31}$/.test(params.uid) || typeof params.region !== "string") throw sourceFailure("CLIENT_PROTOCOL_ERROR");
    if (source === "page:roster") {
      prepared = null;
      const { rows, role } = await owned(tab, signal, params);
      prepared = { rows, role, tab, generation, editing: false };
      return ui.record("genshin", await ui.call(signal, () => tab.url()), "owned-selector", { rows }, generation);
    }
    if (!prepared || prepared.tab !== tab || prepared.generation !== generation) throw sourceFailure("SOURCE_DATA_INVALID");
    ui.assertIdentity(prepared.role, params);
    const rows = prepared.rows;
    if (!Array.isArray(params.names) || !params.names.length || params.names.length > 4 ||
      new Set(params.names).size !== params.names.length || params.names.some(n => !rows.some(r => r.name === n))) {
      throw sourceFailure("SOURCE_DATA_INVALID");
    }
    if (prepared.editing) {
      const current = new URL(await ui.call(signal, () => tab.url()));
      if (current.hash !== "#/character/edit") throw sourceFailure("SOURCE_DATA_INVALID");
      await measure("genshin.return-selector", signal, () => tab.back());
      await ui.wait(signal, () => tab.playwright.getByText("已拥有", { exact: true }).isVisible(), Boolean);
      const returned = new URL(await ui.call(signal, () => tab.url()));
      if (returned.hash !== "#/character") throw sourceFailure("SOURCE_DATA_INVALID");
      prepared.editing = false;
    }
    const nickname = await ui.call(signal, () => tab.playwright.locator(".user-wrapper .nickname").innerText({ timeoutMs: 1500 }));
    if (nickname.trim() !== prepared.role.nickname) throw sourceFailure("WRONG_ACCOUNT");
    const selectedNames = async () => ui.call(signal, () => tab.playwright.evaluate(() =>
      Array.from(document.querySelectorAll(".character-item")).filter(e => e.querySelector(".gt-avatar-selected"))
        .map(e => e.querySelector("p.gt-mobile-caption-c2-3")?.textContent?.trim())));
    let selected = await selectedNames();
    async function visibleLabel(card, name) {
      const label = card.getByText(name, { exact: true });
      if (!(await ui.call(signal, () => label.isVisible()))) throw sourceFailure("SOURCE_DATA_INVALID");
      const covered = () => label.evaluate(e => {
        const r = e.getBoundingClientRect();
        const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
        return { hover: Boolean(hit?.closest(".character-popper")),
          blocked: Boolean(hit?.closest('[role="dialog"],[aria-modal="true"],.gt-dialog,.gt-modal') &&
            !e.closest(".character-item").contains(hit)) };
      });
      let state = await ui.call(signal, covered);
      if (state.hover) {
        // Only the observed official character hover is dismissed. No generic
        // dialog handling or forced clicks through unknown overlays.
        await measure("genshin.dismiss-hover", signal, () =>
          tab.playwright.getByText("选择角色", { exact: true }).click({ timeoutMs: 400 }));
        state = await ui.wait(signal, covered, value => !value.hover, { timeoutMs: 1500 });
      }
      if (state.blocked) {
        // A returned selector may already be visible while its old modal is
        // finishing a closing transition. Wait for real hit-test readiness;
        // never dismiss an unknown dialog or click through an overlay.
        try {
          state = await measure("genshin.wait-overlay", signal, () =>
            ui.wait(signal, covered, value => !value.blocked && !value.hover, { timeoutMs: 1500 }));
        } catch (error) {
          if (error.code !== "SOURCE_REQUEST_NOT_OBSERVED") throw error;
          throw sourceFailure("SOURCE_DATA_INVALID");
        }
      }
      return label;
    }
    async function selectLabel(name, operation) {
      const card = tab.playwright.locator(".character-item").filter({ has: tab.playwright.getByText(name, { exact: true }) });
      const label = await visibleLabel(card, name);
      // Selection reorders the cards. A completed click is not evidence that
      // the rendered selection marker has updated yet (especially on Windows).
      try { await measure(operation, signal, () => label.click({ timeoutMs: 1500 })); }
      catch (error) {
        if (error.name !== "TimeoutError") throw error;
      }
      try {
        await measure("genshin.wait-selection", signal, () => ui.wait(signal, selectedNames,
          names => names.includes(name) === (operation === "genshin.select"), { timeoutMs: 1500, stable: true }));
      } catch (error) {
        // A genuinely missed action gets only the observed-set repair below.
        if (error.code !== "SOURCE_REQUEST_NOT_OBSERVED") throw error;
      }
    }
    for (const name of selected.filter(n => !params.names.includes(n))) {
      await selectLabel(name, "genshin.deselect");
    }
    const unavailable = [];
    for (const name of params.names.filter(n => !selected.includes(n))) {
      const card = tab.playwright.locator(".character-item").filter({ has: tab.playwright.getByText(name, { exact: true }) });
      if (name.includes("奇偶")) {
        const state = await ui.click(tab, await visibleLabel(card, name), signal);
        if (state.includes(UNSUPPORTED)) unavailable.push({ name, status: "unsupported", reason: UNSUPPORTED });
      } else await selectLabel(name, "genshin.select");
    }
    selected = await selectedNames();
    // Reconcile an observed missed click once, using the name label as the hit
    // target. Never toggle a card again without first reading its selected state.
    let repaired = false;
    for (const name of new Set([...selected, ...params.names])) {
      if (!unavailable.some(u => u.name === name) && selected.includes(name) !== params.names.includes(name)) {
        repaired = true;
        const card = tab.playwright.locator(".character-item").filter({ has: tab.playwright.getByText(name, { exact: true }) });
        const state = name.includes("奇偶") ? await ui.click(tab, await visibleLabel(card, name), signal) :
          (await selectLabel(name, params.names.includes(name) ? "genshin.select" : "genshin.deselect"), "");
        if (params.names.includes(name) && name.includes("奇偶") && state.includes(UNSUPPORTED)) {
          unavailable.push({ name, status: "unsupported", reason: UNSUPPORTED });
        }
      }
    }
    if (repaired) selected = await selectedNames();
    const expected = params.names.filter(n => !unavailable.some(u => u.name === n));
    try {
      selected = await measure("genshin.verify-selection", signal, () => ui.wait(signal, selectedNames,
        names => names.length === expected.length && names.every(n => expected.includes(n)), { timeoutMs: 1500, stable: true }));
    } catch (error) {
      if (error.code !== "SOURCE_REQUEST_NOT_OBSERVED") throw error;
      throw sourceFailure("SOURCE_DATA_INVALID");
    }
    if (!expected.length) return ui.record("genshin", await ui.call(signal, () => tab.url()), "character-selection-and-editor", { details: unavailable }, generation);
    await measure("genshin.confirm", signal, () => ui.click(tab, tab.playwright.getByRole("button", { name: "确认", exact: true }), signal));
    prepared.editing = true;
    const details = await measure("genshin.details", signal, () => ui.wait(signal, () => tab.playwright.evaluate(() =>
      Array.from(document.querySelectorAll(".characters-list > .container")).map(e => {
        const pair = root => Array.from(root?.querySelectorAll("input") ?? []).map(i => i.value);
        const avatar = e.querySelector(".avatar-section"), weapon = e.querySelector(".weapon-section");
        return { name: avatar?.innerText?.split("\n").map(t => t.trim()).filter(Boolean)[0],
          synced: e.querySelector(".sync-weapon-tip")?.textContent?.trim() === "已同步拥有角色的信息",
          levels: pair(avatar), skills: Array.from(e.querySelectorAll(".skill-wrapper")).map(s => ({
            labels: s.innerText.split("\n").map(t => t.trim()).filter(Boolean), levels: pair(s),
          })), weapon: { name: weapon?.innerText?.trim(), levels: pair(weapon) } };
      })), values => values.length === expected.length && values.every(v => v.synced && expected.includes(v.name)), { stable: true }));
    return ui.record("genshin", await ui.call(signal, () => tab.url()), "character-selection-and-editor", { details: [...details, ...unavailable] }, generation);
  }
  return Object.freeze({ read: async query => {
    try { return await read(query); }
    catch (error) { prepared = null; throw error; }
  }, available: async ({ signal }) => await ui.available(PAGE, signal) ? [...sourceNames] : [] });
}
