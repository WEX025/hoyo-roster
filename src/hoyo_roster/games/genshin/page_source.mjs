import { pageSourceNames } from "../../client_page.mjs";
import { sourceFailure } from "../../client_network.mjs";

export const PAGE = "https://act.mihoyo.com/ys/event/calculator/index.html#/character";
export const sourceNames = pageSourceNames;
const UNSUPPORTED = "完成特定任务可达成奇偶突破、升级条件，养成计算器中暂不支持奇偶相关的计算";

export function createGenshinPageSource(ui) {
  async function identity(tab, signal) {
    const role = await ui.identity(tab, signal, async () => {
      await ui.wait(signal, () => tab.playwright.locator(".user-wrapper .switch").count(), n => n === 1);
      await ui.click(tab, tab.playwright.locator(".user-wrapper .switch"), signal);
    });
    const nickname = await ui.call(signal, () => tab.playwright.locator(".user-wrapper .nickname").innerText({ timeoutMs: 1500 }));
    if (nickname.trim() !== role.nickname) throw sourceFailure("WRONG_ACCOUNT");
    return role;
  }
  async function owned(tab, signal, params) {
    await ui.call(signal, () => tab.reload());
    await ui.wait(signal, () => tab.playwright.evaluate(() => Boolean(document.querySelector(".user-wrapper .switch"))), Boolean);
    ui.assertIdentity(await identity(tab, signal), params);
    const hasOwned = await ui.call(signal, () => tab.playwright.getByText("已拥有", { exact: true }).isVisible());
    if (!hasOwned) await ui.click(tab, tab.playwright.getByText("角色", { exact: true }), signal);
    await ui.wait(signal, () => tab.playwright.getByText("已拥有", { exact: true }).isVisible(), Boolean);
    const active = await ui.call(signal, () => tab.playwright.getByText("已拥有", { exact: true })
      .evaluate(e => e.parentElement.classList.contains("active")));
    if (!active) await ui.click(tab, tab.playwright.getByText("已拥有", { exact: true }), signal);
    return ui.wait(signal, () => tab.playwright.evaluate(() =>
      Array.from(document.querySelectorAll(".character-item")).map(e => ({
        name: e.querySelector("p.gt-mobile-caption-c2-3")?.textContent?.trim(),
        level: e.querySelector(".gt-avatar-name p")?.textContent?.trim(),
      }))), rows => rows.length > 0 && rows.every(r => r.name && /^Lv\.[1-9][0-9]*$/.test(r.level ?? "")), { stable: true });
  }
  async function read({ source, params, body, signal, generation }) {
    if (body !== null || !params || typeof params !== "object" || Array.isArray(params)) throw sourceFailure("CLIENT_PROTOCOL_ERROR");
    const tab = await ui.page(PAGE, signal);
    if (source === "page:identity") {
      if (Object.keys(params).length) throw sourceFailure("CLIENT_PROTOCOL_ERROR");
      const role = await identity(tab, signal);
      return ui.record("genshin", await ui.call(signal, () => tab.url()), "role-selector", role, generation);
    }
    const fields = source === "page:detail" ? "names,region,uid" : "region,uid";
    if (!sourceNames.includes(source) || Object.keys(params).sort().join(",") !== fields ||
      !/^[1-9][0-9]{0,31}$/.test(params.uid) || typeof params.region !== "string") throw sourceFailure("CLIENT_PROTOCOL_ERROR");
    const rows = await owned(tab, signal, params);
    if (source === "page:roster") return ui.record("genshin", await ui.call(signal, () => tab.url()), "owned-selector", { rows }, generation);
    if (!Array.isArray(params.names) || !params.names.length || params.names.length > 8 ||
      new Set(params.names).size !== params.names.length || params.names.some(n => !rows.some(r => r.name === n))) {
      throw sourceFailure("SOURCE_DATA_INVALID");
    }
    const selectedNames = async () => ui.call(signal, () => tab.playwright.evaluate(() =>
      Array.from(document.querySelectorAll(".character-item")).filter(e => e.querySelector(".gt-avatar-selected"))
        .map(e => e.querySelector("p.gt-mobile-caption-c2-3")?.textContent?.trim())));
    let selected = await selectedNames();
    for (const name of selected.filter(n => !params.names.includes(n))) {
      const card = tab.playwright.locator(".character-item").filter({ has: tab.playwright.getByText(name, { exact: true }) });
      await ui.call(signal, () => card.click({ timeoutMs: 2500 }));
    }
    const unavailable = [];
    for (const name of params.names.filter(n => !selected.includes(n))) {
      const card = tab.playwright.locator(".character-item").filter({ has: tab.playwright.getByText(name, { exact: true }) });
      if (name.includes("奇偶")) {
        const state = await ui.click(tab, card.getByText(name, { exact: true }), signal);
        if (state.includes(UNSUPPORTED)) unavailable.push({ name, status: "unsupported", reason: UNSUPPORTED });
      } else await ui.call(signal, () => card.click({ timeoutMs: 2500 }));
    }
    await ui.call(signal, () => tab.playwright.domSnapshot());
    selected = await selectedNames();
    // Reconcile an observed missed click once, using the name label as the hit
    // target. Never toggle a card again without first reading its selected state.
    for (const name of new Set([...selected, ...params.names])) {
      if (!unavailable.some(u => u.name === name) && selected.includes(name) !== params.names.includes(name)) {
        const card = tab.playwright.locator(".character-item").filter({ has: tab.playwright.getByText(name, { exact: true }) });
        const state = await ui.click(tab, card.getByText(name, { exact: true }), signal);
        if (params.names.includes(name) && name.includes("奇偶") && state.includes(UNSUPPORTED)) {
          unavailable.push({ name, status: "unsupported", reason: UNSUPPORTED });
        }
      }
    }
    selected = await selectedNames();
    const expected = params.names.filter(n => !unavailable.some(u => u.name === n));
    if (selected.length !== expected.length || selected.some(n => !expected.includes(n))) throw sourceFailure("SOURCE_DATA_INVALID");
    if (!expected.length) return ui.record("genshin", await ui.call(signal, () => tab.url()), "character-selection-and-editor", { details: unavailable }, generation);
    await ui.click(tab, tab.playwright.getByRole("button", { name: "确认", exact: true }), signal);
    const details = await ui.wait(signal, () => tab.playwright.evaluate(() =>
      Array.from(document.querySelectorAll(".characters-list > .container")).map(e => {
        const pair = root => Array.from(root?.querySelectorAll("input") ?? []).map(i => i.value);
        const avatar = e.querySelector(".avatar-section"), weapon = e.querySelector(".weapon-section");
        return { name: avatar?.innerText?.split("\n").map(t => t.trim()).filter(Boolean)[0],
          synced: e.querySelector(".sync-weapon-tip")?.textContent?.trim() === "已同步拥有角色的信息",
          levels: pair(avatar), skills: Array.from(e.querySelectorAll(".skill-wrapper")).map(s => ({
            labels: s.innerText.split("\n").map(t => t.trim()).filter(Boolean), levels: pair(s),
          })), weapon: { name: weapon?.innerText?.trim(), levels: pair(weapon) } };
      })), values => values.length === expected.length && values.every(v => v.synced && expected.includes(v.name)), { stable: true });
    return ui.record("genshin", await ui.call(signal, () => tab.url()), "character-selection-and-editor", { details: [...details, ...unavailable] }, generation);
  }
  return Object.freeze({ read, available: async ({ signal }) => await ui.available(PAGE, signal) ? [...sourceNames] : [] });
}
