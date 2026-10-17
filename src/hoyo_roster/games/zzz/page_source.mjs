import { pageSourceNames, readDom } from "../../client_page.mjs";
import { sourceFailure } from "../../client_network.mjs";

export const PAGE = "https://act.mihoyo.com/zzz/gt/character-builder-h/index.html#/";
export const sourceNames = pageSourceNames;

export async function dismissSchemeNotice(ui, tab, signal) {
  const notice = tab.playwright.getByRole("heading", { name: "方案更新提示", exact: true });
  if (!(await ui.call(signal, () => notice.isVisible()))) return false;
  const acknowledge = tab.playwright.getByRole("button", { name: "知道了", exact: true });
  if (await ui.call(signal, () => acknowledge.isVisible())) {
    try { await ui.click(tab, acknowledge, signal, { snapshot: false }); }
    catch (error) {
      // The known notice may finish closing between the visibility check and
      // click. Only that observed disappearance is recoverable here.
      if (await ui.call(signal, () => acknowledge.isVisible())) throw error;
    }
  }
  await ui.wait(signal, () => notice.isVisible(), visible => !visible, { timeoutMs: 5000 });
  return true;
}

export function createZzzPageSource(ui) {
  let roster = null, epoch = null;
  let identityContext = null;
  const measure = (name, signal, action) => ui.measure(name, signal, action);
  async function click(tab, locator, signal) {
    await dismissSchemeNotice(ui, tab, signal);
    try { return await measure("zzz.click", signal, () => ui.click(tab, locator, signal, { snapshot: false })); }
    catch (error) {
      if (!(await dismissSchemeNotice(ui, tab, signal))) throw error;
      return measure("zzz.click", signal, () => ui.click(tab, locator, signal, { snapshot: false }));
    }
  }
  async function openPanel(tab, locator, ready, signal) {
    const observedReady = async () => {
      await dismissSchemeNotice(ui, tab, signal);
      return ready();
    };
    if (await ui.call(signal, ready)) return;
    await click(tab, locator, signal);
    try { await ui.wait(signal, observedReady, Boolean, { timeoutMs: 5000 }); }
    catch (error) {
      if (error.code !== "SOURCE_REQUEST_NOT_OBSERVED") throw error;
      await dismissSchemeNotice(ui, tab, signal);
      if (!(await ui.call(signal, ready))) await click(tab, locator, signal);
      await ui.wait(signal, observedReady, Boolean);
    }
  }
  const headerName = tab => readDom(tab.playwright, () =>
    Array.from(document.querySelectorAll("div.relative.z-2")).find(e => e.parentElement.classList.contains("mr-4"))?.textContent?.trim() ?? "");
  async function identity(tab, signal) {
    await dismissSchemeNotice(ui, tab, signal);
    return ui.identity(tab, signal, async () => {
      await ui.authenticatedControl(tab, ".account-center-icon", signal);
      await openPanel(tab, tab.playwright.locator(".account-center-icon"),
        () => tab.playwright.getByText("切换角色", { exact: true }).isVisible(), signal);
      await openPanel(tab, tab.playwright.getByText("切换角色", { exact: true }),
        () => tab.playwright.locator(".game-role-list").isVisible(), signal);
    });
  }
  async function selector(tab, signal, scan = true) {
    await dismissSchemeNotice(ui, tab, signal);
    const isOpen = () => tab.playwright.getByText("选择代理人", { exact: true }).isVisible();
    await openPanel(tab, tab.playwright.locator("svg.more-icon"), isOpen, signal);
    if (!scan) return;
    return measure("zzz.roster", signal, () => ui.wait(signal, () => readDom(tab.playwright, () =>
      Array.from(document.querySelectorAll(".agent-list__row .gt-card")).map(e => ({
        name: e.querySelector(".gt-card__info-outside")?.textContent?.trim(), text: e.innerText?.trim(),
      }))), rows => rows.length > 0 && rows.every(r => r.name && /^(?:Lv\.[1-9][0-9]*|-)\s/.test(r.text ?? "")), { stable: true }));
  }
  async function read({ source, params, body, signal, generation }) {
    if (body !== null || !params || typeof params !== "object" || Array.isArray(params)) throw sourceFailure("CLIENT_PROTOCOL_ERROR");
    const tab = await ui.page(PAGE, signal);
    if (source === "page:identity") {
      roster = null; epoch = null;
      if (Object.keys(params).length) throw sourceFailure("CLIENT_PROTOCOL_ERROR");
      // Reset transient selector/dialog overlays left by a previous interrupted
      // collection. Reloading this tab retains the SAME browser login session.
      if (identityContext?.tab !== tab || identityContext.generation !== generation) {
        await measure("zzz.reload", signal, () => tab.reload());
      }
      const role = await measure("zzz.identity", signal, () => identity(tab, signal));
      identityContext = { tab, generation };
      return ui.record("zzz", await ui.call(signal, () => tab.url()), "role-selector", role, generation);
    }
    const fields = source === "page:detail" ? "names,region,uid" : "region,uid";
    if (!sourceNames.includes(source) || Object.keys(params).sort().join(",") !== fields ||
      !/^[1-9][0-9]{0,31}$/.test(params.uid) || typeof params.region !== "string") throw sourceFailure("CLIENT_PROTOCOL_ERROR");
    if (source === "page:roster") {
      roster = null; epoch = null;
      await measure("zzz.reload", signal, () => tab.reload());
      ui.assertIdentity(await measure("zzz.identity", signal, () => identity(tab, signal)), params);
      identityContext = { tab, generation };
      const cards = await selector(tab, signal);
      roster = cards.filter(r => /^Lv\./.test(r.text)).map(r => ({ name: r.name, level: r.text.match(/^Lv\.[0-9]+/)[0] }));
      epoch = { uid: params.uid, region: params.region, generation, tab };
      return ui.record("zzz", await ui.call(signal, () => tab.url()), "owned-selector", { rows: roster, excluded_unowned: cards.length - roster.length }, generation);
    }
    if (!roster || epoch?.generation !== generation || epoch.tab !== tab || epoch.uid !== params.uid || epoch.region !== params.region ||
      !Array.isArray(params.names) || params.names.length !== 1 || !roster.some(r => r.name === params.names[0])) {
      throw sourceFailure("SOURCE_DATA_INVALID");
    }
    const name = params.names[0];
    const previousHeader = await ui.call(signal, () => headerName(tab));
    await measure("zzz.open-selector", signal, () => selector(tab, signal, false));
    const card = tab.playwright.locator(".agent-list__row .gt-card").filter({ has: tab.playwright.getByText(name, { exact: true }) });
    await ui.wait(signal, () => card.isVisible(), Boolean);
    const ownedLabel = await ui.call(signal, () => card.innerText({ timeoutMs: 1500 }));
    if (!/^Lv\.[1-9][0-9]*\s/.test(ownedLabel)) throw sourceFailure("SOURCE_DATA_INVALID");
    // The official visible active marker associates an unchanged header with
    // this owned card. Re-selecting it closes the panel without a title change.
    const targetWasActive = await ui.call(signal, () => readDom(card, e => Boolean(e.querySelector(".gt-card__content.active"))));
    const selectorOpen = () => tab.playwright.getByText("选择代理人", { exact: true }).isVisible();
    await click(tab, card.getByText(name, { exact: true }), signal);
    await dismissSchemeNotice(ui, tab, signal);
    const transition = await ui.call(signal, () => readDom(tab.playwright, () =>
      !Array.from(document.querySelectorAll("p,div")).some(e => !e.children.length &&
        e.textContent?.trim() === "当前技能等级" && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0)));
    try { await ui.wait(signal, selectorOpen, open => !open, { timeoutMs: 750 }); }
    catch (error) {
      if (error.code !== "SOURCE_REQUEST_NOT_OBSERVED") throw error;
      if (await ui.call(signal, selectorOpen)) await click(tab, card.getByText(name, { exact: true }), signal);
      await ui.wait(signal, selectorOpen, open => !open, { timeoutMs: 3000 });
    }
    const detail = await measure("zzz.details", signal, () => ui.wait(signal, () => readDom(tab.playwright, () => {
      const find = text => Array.from(document.querySelectorAll("p,div")).find(e => e.textContent?.trim() === text && e.children.length === 0 && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
      const name = Array.from(document.querySelectorAll("div.relative.z-2")).find(e => e.parentElement.classList.contains("mr-4"))?.textContent?.trim();
      const weapon = find("当前音擎"), skills = find("当前技能等级"), discs = find("当前装备驱动盘");
      const emptyWeapon = Array.from(document.querySelectorAll(".point-bg-hoc")).some(e =>
        e.previousElementSibling?.textContent?.trim() === "音擎" &&
        e.querySelector('.point-bg-container > .pb-12 > [data-invalid="true"]')?.textContent?.trim() === "EMPTY");
      let discText = discs?.closest(".equip-effect")?.innerText;
      if (!discText && discs) {
        for (let p = discs; p; p = p.parentElement) {
          if (p.innerText?.includes("尚未装备驱动盘")) { discText = p.innerText.split("驱动盘推荐")[0].trim(); break; }
        }
      }
      return { name, current_weapon: weapon?.parentElement.innerText ?? (emptyWeapon ? "EMPTY" : null),
        current_skills: skills?.parentElement.parentElement.innerText, current_discs: discText,
        preview: document.body.innerText.includes("未拥有该代理人，正在预览方案"),
        selector_open: Array.from(document.querySelectorAll(".agent-list__row")).some(e =>
          e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0) };
    }), value => value.name && (value.name !== previousHeader || targetWasActive || transition) && !value.preview && !value.selector_open && value.current_weapon && value.current_skills && value.current_discs, { stable: true }));
    return ui.record("zzz", await ui.call(signal, () => tab.url()), "current-agent-sections", { details: [{ name, display_name: detail.name,
      current_weapon: detail.current_weapon, current_skills: detail.current_skills, current_discs: detail.current_discs }] }, generation);
  }
  async function readWithRecovery(query) {
    try { return await read(query); }
    catch (error) {
      if (error.code && error.code !== "SOURCE_REQUEST_NOT_OBSERVED") throw error;
      const tab = await ui.page(PAGE, query.signal);
      if (!(await ui.call(query.signal, () => tab.playwright.getByText("出错了，请重试", { exact: true }).isVisible()))) throw error;
      await ui.click(tab, tab.playwright.getByText("重试", { exact: true }), query.signal);
      await ui.wait(query.signal, () => tab.playwright.locator(".account-center-icon").count(), n => n === 1);
      if (query.source !== "page:identity") ui.assertIdentity(await identity(tab, query.signal), query.params);
      return read(query); // One explicit official retry; never recurse or relogin.
    }
  }
  return Object.freeze({ read: readWithRecovery, available: async ({ signal }) => await ui.available(PAGE, signal) ? [...sourceNames] : [] });
}
