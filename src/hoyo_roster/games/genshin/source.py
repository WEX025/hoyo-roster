"""Genshin official source records supplied by the same client browser."""

from hoyo_roster.source import ClientSource
from .errors import ApiResponseError, UnsupportedMultiPageContract
from .endpoint_specs import BINDING, CULTIVATION_LIST


class OfficialCollector(ClientSource):
    def __init__(self, session):
        super().__init__(session, "genshin")

    async def binding(self):
        data, _ = await self.read(BINDING, params={"game_biz": "hk4e_cn"})
        if not isinstance(data.get("list"), list):
            raise ApiResponseError("Binding list invalid")
        return data

    async def cultivation_list(self, *, uid, region):
        data, evidence = await self.read(CULTIVATION_LIST, params={"uid": uid, "region": region})
        if not isinstance(evidence, dict) or set(evidence) != {"request", "pages_observed"}:
            raise ApiResponseError("Source request evidence missing")
        body = evidence["request"]
        fields = {"element_attr_ids", "weapon_cat_ids", "page", "size", "uid", "region", "lang"}
        if not isinstance(body, dict) or set(body) != fields:
            raise ApiResponseError("Source request fields invalid")
        if (
            body["uid"] != uid
            or body["region"] != region
            or body["element_attr_ids"] != []
            or body["weapon_cat_ids"] != []
            or type(body["page"]) is not int
            or body["page"] != 1
            or type(body["size"]) is not int
            or body["size"] < 1
            or not isinstance(body["lang"], str)
            or not body["lang"].strip()
        ):
            raise ApiResponseError("Source request identity or filters invalid")
        if (
            type(evidence["pages_observed"]) is not list
            or evidence["pages_observed"] != [1]
            or any(type(page) is not int for page in evidence["pages_observed"])
        ):
            raise UnsupportedMultiPageContract("Unverified multipage source")
        return data, body["size"]
