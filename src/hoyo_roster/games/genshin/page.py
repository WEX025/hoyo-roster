"""Genshin owns mapping of synced UI fields; no guessed entity identifiers."""

from hoyo_roster.errors import SourceDataInvalid
from hoyo_roster.page_source import PageSource
from hoyo_roster.page_snapshot import run_page_game, text, number

PAGE = "https://act.mihoyo.com/ys/event/calculator/index.html#/character"
CATEGORIES = ("普通攻击", "元素战技", "元素爆发")


def normalize(raw, *, stored=False):
    if isinstance(raw, dict) and raw.get("status") == "unsupported":
        if (
            set(raw) != {"name", "status", "reason"}
            or "奇偶" not in text(raw["name"])
            or raw["reason"]
            != "完成特定任务可达成奇偶突破、升级条件，养成计算器中暂不支持奇偶相关的计算"
        ):
            raise SourceDataInvalid()
        return dict(raw)
    if stored:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"name", "level", "skills", "weapon"}
            or type(raw["level"]) is not int
            or not 1 <= raw["level"] <= 9999
            or not isinstance(raw["skills"], list)
            or len(raw["skills"]) != 3
        ):
            raise SourceDataInvalid()
        text(raw["name"])
        for skill, category in zip(raw["skills"], CATEGORIES):
            if (
                not isinstance(skill, dict)
                or set(skill) != {"category", "name", "displayed_level"}
                or skill["category"] != category
                or type(skill["displayed_level"]) is not int
                or not 0 <= skill["displayed_level"] <= 99
            ):
                raise SourceDataInvalid()
            text(skill["name"])
        weapon = raw["weapon"]
        if weapon is not None and (
            not isinstance(weapon, dict)
            or set(weapon) != {"name", "level"}
            or type(weapon["level"]) is not int
            or not 1 <= weapon["level"] <= 9999
        ):
            raise SourceDataInvalid()
        if weapon is not None:
            text(weapon["name"])
        return raw
    if (
        not isinstance(raw, dict)
        or set(raw) != {"name", "synced", "levels", "skills", "weapon"}
        or raw["synced"] is not True
        or not isinstance(raw["levels"], list)
        or len(raw["levels"]) != 2
        or not isinstance(raw["skills"], list)
        or len(raw["skills"]) != 3
        or not isinstance(raw["weapon"], dict)
        or set(raw["weapon"]) != {"name", "levels"}
        or not isinstance(raw["weapon"]["levels"], list)
        or not (
            len(raw["weapon"]["levels"]) == 2
            or (raw["weapon"]["name"] == "点击添加" and raw["weapon"]["levels"] == [])
        )
    ):
        raise SourceDataInvalid()
    result = {
        "name": text(raw["name"]),
        "level": number(raw["levels"][0]),
        "skills": [],
        "weapon": None
        if raw["weapon"]["levels"] == []
        else {
            "name": text(raw["weapon"]["name"]),
            "level": number(raw["weapon"]["levels"][0]),
        },
    }
    for skill, category in zip(raw["skills"], CATEGORIES):
        if (
            not isinstance(skill, dict)
            or set(skill) != {"labels", "levels"}
            or not isinstance(skill["labels"], list)
            or len(skill["labels"]) != 2
            or skill["labels"][0] != category
            or not isinstance(skill["levels"], list)
            or len(skill["levels"]) != 2
        ):
            raise SourceDataInvalid()
        result["skills"].append(
            {
                "category": category,
                "name": text(skill["labels"][1]),
                "displayed_level": number(skill["levels"][0]),
            }
        )
    return normalize(result, stored=True)


async def run(args, session):
    return await run_page_game(
        args,
        session,
        game="genshin",
        batch_size=4,
        normalize=normalize,
        source=PageSource(
            session,
            "genshin",
            PAGE,
            "character-selection-and-editor",
            fragments=("/", "/character", "/character/edit"),
        ),
    )
