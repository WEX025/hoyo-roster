// Donor: d2b70ac games/genshin/browser.py OfficialCollector. Browser ownership
// and login remain in the AI client; only this game owns its endpoint semantics.
import { checkSignal, sourceFailure } from "../../client_network.mjs";

const PAGE = "https://act.mihoyo.com/ys/event/calculator/index.html#/character";
const BINDING = { url: "https://passport-api.mihoyo.com/binding/api/getUserGameRolesByLtoken", method: "GET" };
const AVATARS = { url: "https://api-takumi.mihoyo.com/event/e20200928calculate/v1/sync/avatar/list", method: "POST" };
export const sourceNames = Object.freeze(["binding", "cultivation-avatar-list"]);

export function createGenshinSource(network) {
  async function available({ signal }) {
    return await network.available(PAGE, signal) ? [...sourceNames] : [];
  }
  async function read({ source, params, body, signal }) {
    checkSignal(signal);
    if (body !== null || !params || typeof params !== "object" || Array.isArray(params)) throw sourceFailure("CLIENT_PROTOCOL_ERROR");
    if (source === "binding") {
      if (Object.keys(params).join(",") !== "game_biz" || params.game_biz !== "hk4e_cn") {
        throw sourceFailure("CLIENT_PROTOCOL_ERROR");
      }
      const page = await network.pageFor(PAGE, signal);
      return network.request(page, BINDING, signal, { params });
    }
    if (source !== "cultivation-avatar-list" || Object.keys(params).sort().join(",") !== "region,uid" ||
        !/^[1-9][0-9]*$/.test(params.uid) || !["cn_gf01", "cn_qd01"].includes(params.region)) {
      throw sourceFailure("CLIENT_PROTOCOL_ERROR");
    }
    const page = await network.pageFor(PAGE, signal);
    let observed;
    const pages = new Set();
    const record = await network.observe(page, AVATARS, signal,
      () => network.navigate(page, PAGE, signal), { settleMs: 1000, onRequest(request) {
        let value;
        try { value = JSON.parse(request.postData); } catch { throw sourceFailure("SOURCE_DATA_INVALID"); }
        const fields = ["element_attr_ids", "lang", "page", "region", "size", "uid", "weapon_cat_ids"];
        if (!value || Object.keys(value).sort().join(",") !== fields.join(",") ||
            !Array.isArray(value.element_attr_ids) || value.element_attr_ids.length ||
            !Array.isArray(value.weapon_cat_ids) || value.weapon_cat_ids.length ||
            value.uid !== params.uid || value.region !== params.region || value.page !== 1 ||
            !Number.isSafeInteger(value.size) || value.size < 1 || typeof value.lang !== "string" || !value.lang.trim()) {
          throw sourceFailure("SOURCE_DATA_INVALID");
        }
        pages.add(value.page);
        if (observed && fields.some(key => JSON.stringify(observed[key]) !== JSON.stringify(value[key]))) {
          throw sourceFailure("SOURCE_DATA_INVALID");
        }
        observed = value;
      } });
    if (!observed) throw sourceFailure("SOURCE_DATA_INVALID");
    return { ...record, evidence: { request: observed, pages_observed: [...pages].sort() } };
  }
  return Object.freeze({ available, read });
}
