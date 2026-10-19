// Donor: d2b70ac games/zzz/browser.py + transport.py. No cookie inspection or
// clearing is carried over. The browser handles credentials and Set-Cookie itself.
import { createHash, randomInt } from "node:crypto";
import { checkSignal, requireReceipt, sourceFailure } from "../../client_network.mjs";

const PAGE = "https://www.miyoushe.com/zzz/";
const SALT = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs";
const get = url => Object.freeze({ url, method: "GET" });
const post = url => Object.freeze({ url, method: "POST" });
const SPECS = Object.freeze({
  binding: get("https://api-takumi.mihoyo.com/binding/api/getUserGameRolesByCookie"),
  "record:avatar/basic": get("https://api-takumi-record.mihoyo.com/event/game_record_zzz/api/zzz/avatar/basic"),
  "record:avatar/info": get("https://api-takumi-record.mihoyo.com/event/game_record_zzz/api/zzz/avatar/info"),
  "record:buddy/info": get("https://api-takumi-record.mihoyo.com/event/game_record_zzz/api/zzz/buddy/info"),
  "cultivate:user/avatar_basic_list": get("https://api-takumi.mihoyo.com/event/nap_cultivate_tool/user/avatar_basic_list"),
  "cultivate:user/batch_avatar_detail_v2": post("https://api-takumi.mihoyo.com/event/nap_cultivate_tool/user/batch_avatar_detail_v2"),
});
const BADGE = post("https://api-takumi.mihoyo.com/common/badge/v1/login/account");
export const sourceNames = Object.freeze([...Object.keys(SPECS), "cultivate:badge-login"]);

export function generateDs({ body = null, params = {}, timestamp = Math.floor(Date.now() / 1000), nonce = randomInt(100001, 200001) } = {}) {
  const query = Object.keys(params).sort().map(key => `${key}=${params[key]}`).join("&");
  const encoded = body === null ? "" : JSON.stringify(body);
  const digest = createHash("md5").update(`salt=${SALT}&t=${timestamp}&r=${nonce}&b=${encoded}&q=${query}`).digest("hex");
  return `${timestamp},${nonce},${digest}`;
}

function validate(source, params, body) {
  if (!params || typeof params !== "object" || Array.isArray(params)) throw sourceFailure("CLIENT_PROTOCOL_ERROR");
  let fields;
  if (source === "binding") {
    fields = ["game_biz"];
    if (params.game_biz !== "nap_cn") throw sourceFailure("CLIENT_PROTOCOL_ERROR");
  } else if (source.startsWith("record:")) {
    fields = source === "record:avatar/info" ? ["id_list[]", "role_id", "server"] : ["role_id", "server"];
    if (!/^[1-9][0-9]*$/.test(params.role_id) || params.server !== "prod_gf_cn" ||
        (source === "record:avatar/info" && !/^[1-9][0-9]*$/.test(params["id_list[]"]))) throw sourceFailure("CLIENT_PROTOCOL_ERROR");
  } else {
    fields = ["region", "uid"];
    if (!/^[1-9][0-9]*$/.test(params.uid) || params.region !== "prod_gf_cn") throw sourceFailure("CLIENT_PROTOCOL_ERROR");
  }
  if (Object.keys(params).sort().join(",") !== fields.join(",")) throw sourceFailure("CLIENT_PROTOCOL_ERROR");
  if (source === "cultivate:user/batch_avatar_detail_v2") {
    if (!body || Object.keys(body).join(",") !== "avatar_list" || !Array.isArray(body.avatar_list) ||
        !body.avatar_list.length || body.avatar_list.length > 10) throw sourceFailure("CLIENT_PROTOCOL_ERROR");
    const ids = new Set();
    for (const avatar of body.avatar_list) {
      if (!avatar || Object.keys(avatar).sort().join(",") !== "avatar_id,is_teaser,teaser_need_weapon,teaser_sp_skill" ||
          !Number.isSafeInteger(avatar.avatar_id) || avatar.avatar_id < 1 || ids.has(avatar.avatar_id) ||
          avatar.is_teaser !== false || avatar.teaser_need_weapon !== false || avatar.teaser_sp_skill !== false) {
        throw sourceFailure("CLIENT_PROTOCOL_ERROR");
      }
      ids.add(avatar.avatar_id);
    }
  } else if (body !== null) throw sourceFailure("CLIENT_PROTOCOL_ERROR");
}

export function createZzzSource(network) {
  let headers = null, epoch = null;
  async function available({ signal }) { return await network.available(PAGE, signal) ? [...sourceNames] : []; }
  async function context(signal, generation) {
    const page = await network.pageFor(PAGE, signal);
    if (epoch !== generation) { headers = null; epoch = generation; }
    if (!headers) headers = await network.deviceHeaders(page, PAGE, signal);
    checkSignal(signal);
    return page;
  }
  function requestHeaders(source, body, params) {
    const result = { DS: generateDs({ body, params }), "x-rpc-app_version": "2.11.1",
      "x-rpc-client_type": "5", "x-rpc-lang": "zh-cn", "x-rpc-language": "zh-cn" };
    for (const [name, value] of Object.entries(headers)) {
      if (name === "x-rpc-device_id" && source.startsWith("cultivate:")) continue;
      result[name] = value;
    }
    if (body !== null) result["Content-Type"] = "application/json";
    return result;
  }
  async function read({ source, params, body, signal, generation }) {
    if (!Object.hasOwn(SPECS, source)) throw sourceFailure("CLIENT_PROTOCOL_ERROR");
    validate(source, params, body);
    const page = await context(signal, generation);
    return network.request(page, SPECS[source], signal, { params, body, headers: requestHeaders(source, body, params) });
  }
  async function prepare({ service, params, signal, generation }) {
    if (service !== "cultivate") throw sourceFailure("CLIENT_PROTOCOL_ERROR");
    validate("cultivate:user/avatar_basic_list", params, null);
    const body = { game_biz: "nap_cn", lang: "zh-cn", region: params.region, uid: params.uid };
    const page = await context(signal, generation);
    const record = await network.request(page, BADGE, signal,
      { body, headers: requestHeaders("cultivate:badge-login", body, {}) });
    requireReceipt(record);
    // Never send the initialization body, cookies or auth material over the pipe.
    return { ready: true, origin: record.origin };
  }
  return Object.freeze({ available, read, prepare });
}
