// Public UI operations only; this layer has no game imports or authentication storage.
import { checkSignal, sourceFailure } from "./client_network.mjs";
import { createMetrics } from "./client_metrics.mjs";

export const pageSourceNames = Object.freeze(["page:identity", "page:roster", "page:detail"]);

export function createPageBrowser(browser, metrics = createMetrics()) {
  const retained = new Map();
  let viewportLease = false;
  async function call(signal, action) {
    checkSignal(signal);
    const value = await action();
    checkSignal(signal);
    return value;
  }
  async function page(url, signal) {
    const tabs = await call(signal, () => browser.tabs.list());
    const expected = new URL(url);
    const prior = retained.get(url);
    let found = tabs.find(t => t.id === prior?.id);
    const matches = value => {
      try { const u = new URL(value); return u.origin === expected.origin && u.pathname === expected.pathname; }
      catch { return false; }
    };
    if (found && !matches(found.url)) throw sourceFailure("SOURCE_DATA_INVALID");
    found ??= tabs.filter(t => matches(t.url)).at(-1);
    let tab = await call(signal, () => found ? (found.id === prior?.id ? prior : browser.tabs.get(found.id)) : browser.tabs.new());
    if (![tab.playwright?.evaluate, tab.playwright?.domSnapshot, tab.playwright?.locator,
      tab.playwright?.getByText].every(f => typeof f === "function")) {
      if (!found) await call(signal, () => tab.close());
      throw sourceFailure("CLIENT_SOURCE_UNAVAILABLE");
    }
    if (!found) await call(signal, () => tab.goto(url));
    let size = await call(signal, () => tab.playwright.evaluate(() => ({
      width: document.documentElement.clientWidth, height: document.documentElement.clientHeight,
    })));
    if (found && (!size.width || !size.height)) {
      // Retained panel webviews can become unmounted. A new tab in this SAME
      // browser shares its live session; do not copy credentials or close user tabs.
      tab = await call(signal, () => browser.tabs.new());
      await call(signal, () => tab.goto(url));
      await call(signal, () => tab.playwright.domSnapshot());
      size = await call(signal, () => tab.playwright.evaluate(() => ({
        width: document.documentElement.clientWidth, height: document.documentElement.clientHeight,
      })));
    }
    if (!size.width || !size.height) {
      const capabilities = await call(signal, () => browser.capabilities?.list?.() ?? []);
      if (!capabilities.some(c => c.id === "viewport")) throw sourceFailure("CLIENT_PAGE_UNAVAILABLE");
      const viewport = await call(signal, () => browser.capabilities.get("viewport"));
      // A zero-sized webview cannot receive UI actions. This is a temporary layout
      // repair, never a device-emulation or screenshot-size preference.
      await call(signal, () => viewport.set({ width: 1280, height: 900 }));
      viewportLease = true;
      const repaired = await call(signal, () => tab.playwright.evaluate(() => ({
        width: document.documentElement.clientWidth, height: document.documentElement.clientHeight,
      })));
      if (!repaired.width || !repaired.height) throw sourceFailure("CLIENT_PAGE_UNAVAILABLE");
    }
    if (!matches(await call(signal, () => tab.url()))) throw sourceFailure("SOURCE_DATA_INVALID");
    if (tab !== prior) await call(signal, () => tab.markHandoff());
    retained.set(url, tab);
    return tab;
  }
  async function available(url, signal) {
    try { await page(url, signal); return true; }
    catch (e) { if (e.code === "CLIENT_SOURCE_UNAVAILABLE") return false; throw e; }
  }
  async function wait(signal, read, accept, { timeoutMs = 18000, stable = false } = {}) {
    const deadline = Date.now() + timeoutMs;
    let prior;
    while (Date.now() < deadline) {
      const value = await call(signal, read);
      if (accept(value)) {
        const encoded = JSON.stringify(value);
        if (!stable || encoded === prior) return value;
        prior = encoded;
      } else prior = undefined;
      await call(signal, () => new Promise(resolve => setTimeout(resolve, 150)));
    }
    throw sourceFailure("SOURCE_REQUEST_NOT_OBSERVED");
  }
  async function click(tab, locator, signal, { snapshot = true } = {}) {
    await metrics.measure("page.click", () => call(signal, () => locator.click({ timeoutMs: 2500 })));
    if (snapshot) return metrics.measure("page.snapshot", () => call(signal, () => tab.playwright.domSnapshot()));
  }
  async function authentication(tab, signal) {
    // Only read rendered text and field presence, never any login input value.
    return call(signal, () => tab.playwright.evaluate(() => {
      const fields = Array.from(document.querySelectorAll("input"));
      const visible = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== "hidden";
      return fields.some(e => visible(e) && /手机号|验证码/.test(e.getAttribute("placeholder") ?? "")) ||
        Array.from(document.querySelectorAll("button,a,div,span,p")).some(e => visible(e) && !e.children.length &&
          ["登录", "去登录", "立即登录", "你还没有登录哦", "登录后可查看已有角色数据",
            "需要先登录才能使用养成指南工具"].includes(e.innerText?.trim()));
    }));
  }
  async function authenticatedControl(tab, selector, signal) {
    return wait(signal, async () => {
      if (await authentication(tab, signal)) throw sourceFailure("AUTHENTICATION_REQUIRED");
      return call(signal, () => tab.playwright.locator(selector).count());
    }, count => count === 1);
  }
  async function identity(tab, signal, open) {
    if (await authentication(tab, signal)) throw sourceFailure("AUTHENTICATION_REQUIRED");
    try {
      if (!(await call(signal, () => tab.playwright.locator(".game-role-list").isVisible()))) await open();
    }
    catch (error) {
      if (await authentication(tab, signal)) throw sourceFailure("AUTHENTICATION_REQUIRED");
      throw error;
    }
    const roles = await wait(signal, () => tab.playwright.evaluate(() =>
      Array.from(document.querySelectorAll(".game-role-list > li")).map(e => ({
        uid: e.querySelector(".role-uid")?.textContent?.trim().replace(/^UID\s*/, ""),
        nickname: e.querySelector(".role-name")?.textContent?.trim(),
        region: e.querySelector(".role-region")?.textContent?.trim(),
      }))), rows => rows.length > 0);
    await click(tab, tab.playwright.locator(".mhy-game-role-message-box-header-close-icon"), signal, { snapshot: false });
    const panelVisible = () => tab.playwright.locator(".game-role-list").isVisible();
    try { await wait(signal, panelVisible, visible => !visible, { timeoutMs: 750 }); }
    catch (error) {
      if (error.code !== "SOURCE_REQUEST_NOT_OBSERVED") throw error;
      if (await call(signal, panelVisible)) {
        await metrics.measure("page.identity-close-repair", () =>
          click(tab, tab.playwright.locator(".mhy-game-role-message-box-header-close-icon"), signal, { snapshot: false }));
      }
      await wait(signal, panelVisible, visible => !visible, { timeoutMs: 3000 });
    }
    if (roles.length !== 1 || roles.some(r => !/^[1-9][0-9]{0,31}$/.test(r.uid ?? "") ||
      !r.nickname || !r.region)) throw sourceFailure("WRONG_ACCOUNT");
    return roles[0];
  }
  function assertIdentity(actual, params) {
    if (actual.uid !== params.uid || actual.region !== params.region) throw sourceFailure("WRONG_ACCOUNT");
  }
  function record(game, url, surface, value, generation) {
    return { json: JSON.stringify(value), origin: { kind: "official-page", game, url },
      evidence: { surface, generation } };
  }
  async function release() {
    if (viewportLease) {
      await (await browser.capabilities.get("viewport")).reset();
      viewportLease = false;
    }
  }
  const measure = (operation, signal, action) => metrics.measure(operation, () => call(signal, action));
  return Object.freeze({ page: (url, signal) => measure("page.preflight", signal, () => page(url, signal)),
    available, call, wait, click, authentication, authenticatedControl, identity, assertIdentity, record, release, measure });
}
