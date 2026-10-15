"""Synthetic official-shaped sources delivered through the production client bridge."""

import json
from collections import Counter

from hoyo_roster.client import ClientSession

UID_A = "123456789"
UID_B = "987654321"


def binding_payload(game, uid=UID_A, *, retcode=0):
    return {
        "retcode": retcode,
        "data": {
            "list": [
                {
                    "game_biz": {"genshin": "hk4e_cn", "zzz": "nap_cn"}[game],
                    "game_uid": uid,
                    "region": "cn_gf01" if game == "genshin" else "prod_gf_cn",
                    "nickname": "Synthetic",
                    "level": 50,
                }
            ]
        },
    }


def avatar(level=90):
    return {
        "id": 10000005,
        "name": "Synthetic Traveler",
        "avatar_level": 5,
        "level_current": level,
        "promote_level": 6,
        "constellation_num": 0,
        "fetter_level": 1,
        "element_attr_id": 3,
        "weapon": None,
        "skill_list": [
            {"id": 100545, "group_id": 730, "level_current": 5, "max_level": 10},
            {"id": 10077, "group_id": 932, "level_current": 6, "max_level": 10},
            {"id": 10078, "group_id": 939, "level_current": 7, "max_level": 10},
        ],
    }


class FakePort:
    def __init__(self, *, authenticated=True, source_reader=True):
        self.ref = "a" * 32
        self.authenticated = authenticated
        self.source_reader = source_reader
        self.calls = []
        self.counts = Counter()
        self.answers = ["completed"]
        self.fail_at = {}
        self.drift = False
        self.empty_games = set()
        self.uid = UID_A
        self.login_changes_account = False

    def client(self):
        return ClientSession(self)

    async def exchange(self, message):
        self.calls.append(message)
        method, params = message["method"], message["params"]
        result, error = None, None
        if method == "session.attach":
            result = {"browser": "client-built-in", "source_reader": self.source_reader}
        elif method == "login.open":
            result = {"page": "synthetic-page"}
        elif method == "login.confirm":
            result = self.answers.pop(0) if self.answers else "completed"
            if result == "completed":
                self.authenticated = True
                if self.login_changes_account:
                    self.uid = UID_B
        elif method == "source.read":
            game, source = params["game"], params["source"]
            self.counts[(game, source)] += 1
            number = self.counts[(game, source)]
            error = self.fail_at.get((game, source, number))
            if not self.authenticated:
                error = "AUTHENTICATION_REQUIRED"
            if error is None:
                payload, evidence = self.source(game, source, params["params"], params["body"])
                result = {
                    "json": json.dumps(payload, ensure_ascii=False),
                    "evidence": evidence,
                    "origin": self.origin(game, source),
                }
        else:
            raise AssertionError(method)
        return {
            "version": 1,
            "id": message["id"],
            "session": self.ref,
            "result": result,
            "error": error,
        }

    @staticmethod
    def origin(game, source):
        if game == "genshin":
            from hoyo_roster.games.genshin.endpoint_specs import BINDING, CULTIVATION_LIST

            spec = {BINDING.name: BINDING, CULTIVATION_LIST.name: CULTIVATION_LIST}[source]
        else:
            from hoyo_roster.games.zzz.endpoint_specs import ENDPOINT_REGISTRY

            spec = ENDPOINT_REGISTRY[source]
        return {"url": spec.url, "method": spec.method, "status": 200, "redirected": False}

    def source(self, game, source, params, body):
        if source == "binding":
            payload = binding_payload(game, self.uid)
            if game in self.empty_games:
                payload["data"]["list"] = []
            return payload, None
        if game == "genshin":
            assert source == "cultivation-avatar-list"
            level = 89 if self.drift and self.counts[(game, source)] % 2 == 0 else 90
            data = {"list": [avatar(level)], "total": 1}
            evidence = {
                "request": {
                    "uid": self.uid,
                    "region": "cn_gf01",
                    "lang": "zh-cn",
                    "element_attr_ids": [],
                    "weapon_cat_ids": [],
                    "page": 1,
                    "size": 100,
                },
                "pages_observed": [1],
            }
            return {"retcode": 0, "data": data}, evidence
        from hoyo_roster.games.zzz.normalize import SKILL_TYPE_FIELDS, REQUIRED_PANEL_PROPERTY_IDS

        if source == "record:avatar/basic":
            data = {
                "avatar_list": [{"id": 1011, "name": "Synthetic Agent", "level": 20, "rank": 1}]
            }
        elif source == "cultivate:user/avatar_basic_list":
            data = {"list": [{"avatar": {"id": 1011}, "unlocked": True, "is_teaser": False}]}
        elif source == "record:avatar/info":
            data = {
                "avatar_list": [
                    {
                        "id": 1011,
                        "skills": [{"skill_type": i, "level": 1} for i in SKILL_TYPE_FIELDS],
                        "properties": [
                            {
                                "property_id": i,
                                "final": "5.1234567890123456789%" if i == 5 else "100",
                            }
                            for i in REQUIRED_PANEL_PROPERTY_IDS
                        ],
                        "weapon": None,
                        "equip": [],
                    }
                ]
            }
        elif source == "cultivate:user/batch_avatar_detail_v2":
            data = {"list": [{"avatar": {"id": 1011, "promotes": 2}}]}
        elif source == "record:buddy/info":
            data = {"list": []}
        else:
            raise AssertionError(source)
        return {"retcode": 0, "data": data}, None
