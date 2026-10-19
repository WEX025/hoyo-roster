"""ZZZ owns current-section mapping, excluding all recommendation/preview data."""

from hoyo_roster.errors import SourceDataInvalid
from hoyo_roster.page_source import PageSource
from hoyo_roster.page_snapshot import run_page_game, text

PAGE = "https://act.mihoyo.com/zzz/gt/character-builder-h/index.html#/"
MARKERS = {
    "current_weapon": "当前音擎",
    "current_skills": "当前技能等级",
    "current_discs": "当前装备驱动盘",
}


def normalize(raw, *, stored=False):
    if not isinstance(raw, dict) or set(raw) != {"name", "display_name", *MARKERS}:
        raise SourceDataInvalid()
    text(raw["name"])
    text(raw["display_name"])
    result = dict(raw)
    for key, marker in MARKERS.items():
        if key == "current_weapon" and (
            (not stored and raw[key] == "EMPTY") or (stored and raw[key] is None)
        ):
            result[key] = None
            continue
        value = text(raw[key])
        if not value.startswith(marker) or any(
            s in value for s in ("未拥有该代理人", "音擎推荐", "驱动盘推荐", "技能升级建议")
        ):
            raise SourceDataInvalid()
    return result


async def run(args, session):
    return await run_page_game(
        args,
        session,
        game="zzz",
        batch_size=1,
        normalize=normalize,
        source=PageSource(session, "zzz", PAGE, "current-agent-sections"),
    )
