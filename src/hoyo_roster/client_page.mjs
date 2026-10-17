// Public UI operations only; this layer has no game imports or authentication storage.
import { checkSignal, sourceFailure } from "./client_network.mjs";
import { createMetrics } from "./client_metrics.mjs";

export const pageSourceNames = Object.freeze(["page:identity", "page:roster", "page:detail"]);

// Bound the client's supported DOM scope setup as well as script execution.
// The source deadline still owns cancellation and drains pending UI calls.
export function readDom(target, read, argument) {
  return target.evaluate(read, argument, { timeoutMs: 5000 });
}

export function createPageBrowser(browser, metrics = createMetrics()) {
  const retained = new Map();
  let viewportLease = false;
  async function call(signal, action) {
    checkSignal(signal);
    const value = await action();
    checkSignal(signal);
    return value;
  }
  async function navigate(tab, url, signal, { reload = false } = {}) {
    const expected = new URL(url);
    if (expected.protocol !== "https:") throw sourceFailure("SOURCE_DATA_INVALID");
    const transient = value => !/\bERR_(?:CERT_|SSL_)/.test(value) &&
      /\b(?:ERR_DNS_NO_MATCHING_SUPPORTED_ALPN|ERR_NAME_NOT_RESOLVED|ERR_TIMED_OUT|ERR_CONNECTION_RESET|ERR_CONNECTION_CLOSED|ERR_CONNECTION_TIMED_OUT|ERR_NETWORK_CHANGED|ERR_INTERNET_DISCONNECTED)\b/.test(value);
    const matches = value => {
      try { const actual = new URL(value); return actual.origin === expected.origin && actual.pathname === expected.pathname; }
      catch { return false; }
    };
    const errorPage = value => {
      if (!value?.startsWith("data:text/html")) return false;
      try { return transient(decodeURIComponent(value)); }
      catch { return false; }
    };
    async function landed() {
      const actual = await call(signal, () => tab.url());
      // Inspect only the fixed code in metadata from OUR navigation, not the
      // error page DOM. Unknown redirects/interstitials are never recovered.
      if (errorPage(actual)) return false;
      if (!matches(actual)) throw sourceFailure("SOURCE_DATA_INVALID");
      let visibleURL;
      try {
        visibleURL = await call(signal, () => readDom(tab.playwright, () => location.href));
      } catch (error) {
        checkSignal(signal);
        // reload() can resolve before a delayed error document replaces the
        // old URL. A blocked DOM scope is not itself proof of a network error.
        const current = (await call(signal, () => browser.tabs.list())).find(t => t.id === tab.id);
        if (current && errorPage(current.url)) return false;
        throw error;
      }
      if (!matches(visibleURL)) throw sourceFailure("SOURCE_DATA_INVALID");
      return true;
    }
    try {
      await metrics.measure("page.navigation", () => call(signal, () => reload ? tab.reload() : tab.goto(url)));
    } catch (error) {
      checkSignal(signal);
      if (/\bERR_(?:CERT_|SSL_)/.test(`${error.code ?? ""} ${error.message ?? ""}`)) throw error;
      if (error.code && !transient(error.code)) throw error;
      if (transient(error.message ?? "") || transient(error.code ?? "")) return retry();
      const current = (await call(signal, () => browser.tabs.list())).find(t => t.id === tab.id);
      if (current && errorPage(current.url)) return retry();
      throw error;
    }
    if (!(await landed())) return retry();
    return tab;
    async function retry() {
      return metrics.measure("page.navigation-retry", async () => {
        // Some clients cannot operate a data: error document at all. Open the
        // SAME approved HTTPS address in this browser; retain the original tab.
        tab = await call(signal, () => browser.tabs.new());
        await call(signal, () => tab.goto(url));
        if (!(await landed())) throw sourceFailure("CLIENT_REQUEST_FAILED");
        await call(signal, () => tab.markHandoff());
        retained.set(url, tab);
        return tab;
      });
    }
  }
  async function page(url, signal) {
    const step = (name, action) => metrics.measure(name, () => call(signal, action));
    const tabs = await step("page.tabs-list", () => browser.tabs.list());
    const expected = new URL(url);
    const prior = retained.get(url);
    let found = tabs.find(t => t.id === prior?.id);
    const matches = value => {
      try { const u = new URL(value); return u.origin === expected.origin && u.pathname === expected.pathname; }
      catch { return false; }
    };
    if (found && !matches(found.url)) throw sourceFailure("SOURCE_DATA_INVALID");
    found ??= tabs.filter(t => matches(t.url)).at(-1);
    let tab = await step("page.tab-resolve", () => found ? (found.id === prior?.id ? prior : browser.tabs.get(found.id)) : browser.tabs.new());
    if (![tab.playwright?.evaluate, tab.playwright?.domSnapshot, tab.playwright?.locator,
      tab.playwright?.getByText].every(f => typeof f === "function")) {
      if (!found) await call(signal, () => tab.close());
      throw sourceFailure("CLIENT_SOURCE_UNAVAILABLE");
    }
    if (!found) tab = await navigate(tab, url, signal);
    let size = await step("page.viewport-read", () => readDom(tab.playwright, () => ({
      width: document.documentElement.clientWidth, height: document.documentElement.clientHeight,
    })));
    if (found && (!size.width || !size.height)) {
      // Retained panel webviews can become unmounted. A new tab in this SAME
      // browser shares its live session; do not copy credentials or close user tabs.
      tab = await call(signal, () => browser.tabs.new());
      tab = await navigate(tab, url, signal);
      await call(signal, () => tab.playwright.domSnapshot());
      size = await step("page.viewport-read", () => readDom(tab.playwright, () => ({
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
      const repaired = await step("page.viewport-read", () => readDom(tab.playwright, () => ({
        width: document.documentElement.clientWidth, height: document.documentElement.clientHeight,
      })));
      if (!repaired.width || !repaired.height) throw sourceFailure("CLIENT_PAGE_UNAVAILABLE");
    }
    if (!matches(await step("page.url-read", () => tab.url()))) throw sourceFailure("SOURCE_DATA_INVALID");
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
    return call(signal, () => readDom(tab.playwright, () => {
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
    const roles = await wait(signal, () => readDom(tab.playwright, () =>
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
    available, navigate, call, wait, click, authentication, authenticatedControl, identity, assertIdentity, record, release, measure });
}
