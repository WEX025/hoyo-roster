"""Page observations run through the actual shared session, games and atomic store."""

import json
from copy import deepcopy

import pytest

from hoyo_roster import cli, platform_io as io
from hoyo_roster.errors import SourceDataInvalid
from hoyo_roster.games.genshin.page import normalize as normalize_genshin, PAGE as GENSHIN_PAGE
from hoyo_roster.games.zzz.page import normalize as normalize_zzz, PAGE as ZZZ_PAGE
from hoyo_roster.page_snapshot import PageSnapshotStore, identity, roster
from hoyo_roster.user_workspace import direct_configuration
from tests.fixtures import FakePort, UID_A


@pytest.fixture(autouse=True)
def no_delays(monkeypatch):
    from hoyo_roster.request_gate import RequestGate

    async def reserve(self, **kwargs):
        return 0

    monkeypatch.setattr(RequestGate, "reserve", reserve)


def gs_detail(name="Alpha", level="90"):
    return {
        "name": name,
        "synced": True,
        "levels": [level, "99"],
        "skills": [
            {"labels": [kind, "Synthetic skill"], "levels": [str(n), "15"]}
            for kind, n in zip(("普通攻击", "元素战技", "元素爆发"), (1, 7, 8))
        ],
        "weapon": {"name": "Synthetic weapon", "levels": ["20", "90"]},
    }


class PagePort(FakePort):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.page_drift = False
        self.page_origin = None
        self.unsupported = False

    async def exchange(self, message):
        method, params = message["method"], message["params"]
        if method not in {"session.attach", "source.check", "source.read"}:
            return await super().exchange(message)
        self.calls.append(message)
        error = None
        if method == "session.attach":
            result = {"browser": "client-built-in", "source_reader": True, "page_reader": True}
        elif method == "source.check":
            result = {"available": [s for s in params["sources"] if s.startswith("page:")]}
        else:
            game, source = params["game"], params["source"]
            self.counts[(game, source)] += 1
            count = self.counts[(game, source)]
            error = self.fail_at.get((game, source, count))
            if not self.authenticated:
                error = "AUTHENTICATION_REQUIRED"
            if error:
                result = None
            else:
                operation = source.split(":")[1]
                if operation == "identity":
                    value = {"uid": self.uid, "nickname": "Synthetic", "region": "Synthetic region"}
                    surface = "role-selector"
                elif operation == "roster":
                    value = {"rows": [{"name": "Alpha", "level": "Lv.90"}]}
                    if self.unsupported:
                        value["rows"].append({"name": "奇偶·女性", "level": "Lv.90"})
                    if self.page_drift and count == 2:
                        value["rows"][0]["level"] = "Lv.89"
                    surface = "owned-selector"
                else:
                    value = {
                        "details": [gs_detail()]
                        if game == "genshin"
                        else [
                            {
                                "name": "Alpha",
                                "display_name": "Synthetic long display name",
                                "current_weapon": "当前音擎\nSynthetic\nLv.20",
                                "current_skills": "当前技能等级\n02\n03\n04\n05\n06\n07",
                                "current_discs": "当前装备驱动盘\n尚未装备驱动盘",
                            }
                        ]
                    }
                    surface = (
                        "character-selection-and-editor"
                        if game == "genshin"
                        else "current-agent-sections"
                    )
                    if self.unsupported and game == "genshin":
                        value["details"] = [
                            {
                                "name": name,
                                "status": "unsupported",
                                "reason": "完成特定任务可达成奇偶突破、升级条件，养成计算器中暂不支持奇偶相关的计算",
                            }
                            if "奇偶" in name
                            else gs_detail(name)
                            for name in params["params"]["names"]
                        ]
                result = {
                    "json": json.dumps(value, ensure_ascii=False),
                    "origin": self.page_origin
                    or {
                        "kind": "official-page",
                        "game": game,
                        "url": GENSHIN_PAGE if game == "genshin" else ZZZ_PAGE,
                    },
                    "evidence": {"surface": surface, "generation": 0},
                }
        return {
            "version": 1,
            "id": message["id"],
            "session": self.ref,
            "result": result,
            "error": error,
        }


def arguments(tmp_path, *extra):
    return [
        "--storage-root",
        str(tmp_path / "results"),
        "--runtime-root",
        str(tmp_path / "runtime"),
        *extra,
    ]


def snapshots(tmp_path):
    return sorted(io.filesystem_path(tmp_path / "results").rglob("snapshot.json"))


def test_page_default_shares_session_login_and_saves_two_partial_snapshots(tmp_path, capsys):
    port = PagePort(authenticated=False)
    assert cli.main(arguments(tmp_path), client=port.client()) == 0
    assert sum(c["method"] == "login.confirm" for c in port.calls) == 1
    assert {c["session"] for c in port.calls[1:]} == {port.ref}
    assert len(snapshots(tmp_path)) == 2
    for file in snapshots(tmp_path):
        value = json.loads(file.read_text())
        assert value["state"] == "partial"
        assert value["format"] == "hoyo-roster-page-v1"
        assert value["scope"] == "rendered-owned-characters"
        assert len(value["roster"]) == len(value["details"]) == 1
    assert not (tmp_path / "results" / "game-results-v3").exists()
    output = capsys.readouterr().out
    assert UID_A not in output and "Synthetic" not in output
    assert output.count("COLLECTION_STATUS=PARTIAL") == 2
    assert "TASK_STATUS=PASS" in output
    for game in ("genshin", "zzz"):
        assert port.counts[(game, "page:roster")] == 2


def test_second_game_auth_recovery_preserves_completed_first_game(tmp_path):
    port = PagePort()
    port.fail_at[("zzz", "page:identity", 1)] = "AUTHENTICATION_REQUIRED"
    assert cli.main(arguments(tmp_path), client=port.client()) == 0
    assert sum(c["method"] == "login.confirm" for c in port.calls) == 1
    assert port.counts[("genshin", "page:roster")] == 2
    assert port.counts[("zzz", "page:roster")] == 2
    assert len(snapshots(tmp_path)) == 2


def test_page_recovery_rejects_account_change_after_first_game(tmp_path):
    port = PagePort()
    port.fail_at[("zzz", "page:identity", 1)] = "AUTHENTICATION_REQUIRED"
    port.login_changes_account = True
    assert cli.main(arguments(tmp_path), client=port.client()) == 1
    assert len(snapshots(tmp_path)) == 1
    assert json.loads(snapshots(tmp_path)[0].read_text())["game"] == "genshin"
    assert port.counts[("zzz", "page:roster")] == 0


def test_page_drift_never_writes_a_snapshot(tmp_path):
    port = PagePort()
    port.page_drift = True
    assert cli.main(arguments(tmp_path, "--game", "genshin"), client=port.client()) == 1
    assert not snapshots(tmp_path)


@pytest.mark.parametrize(
    "origin",
    [
        {"url": GENSHIN_PAGE, "method": "GET", "status": 200, "redirected": False},
        {"kind": "official-page", "game": "zzz", "url": GENSHIN_PAGE},
        {"kind": "official-page", "game": "genshin", "url": "https://example.com/"},
    ],
)
def test_page_provenance_rejects_fabricated_api_or_wrong_page(tmp_path, origin):
    port = PagePort()
    port.page_origin = origin
    assert cli.main(arguments(tmp_path, "--game", "genshin"), client=port.client()) == 1
    assert not snapshots(tmp_path)


@pytest.mark.parametrize("route", ["/", "/character", "/character/edit"])
def test_genshin_registered_routes_are_real_page_provenance(tmp_path, route):
    port = PagePort()
    port.page_origin = {
        "kind": "official-page",
        "game": "genshin",
        "url": GENSHIN_PAGE.split("#")[0] + "#" + route,
    }
    assert cli.main(arguments(tmp_path, "--game", "genshin"), client=port.client()) == 0
    assert len(snapshots(tmp_path)) == 1


def test_genshin_unknown_route_is_not_accepted_as_the_registered_page(tmp_path):
    port = PagePort()
    port.page_origin = {
        "kind": "official-page",
        "game": "genshin",
        "url": GENSHIN_PAGE.replace("/character", "/unknown"),
    }
    assert cli.main(arguments(tmp_path, "--game", "genshin"), client=port.client()) == 1
    assert not snapshots(tmp_path)


def test_zzz_keeps_selected_label_and_detail_display_name_separate(tmp_path):
    assert cli.main(arguments(tmp_path, "--game", "zzz"), client=PagePort().client()) == 0
    detail = json.loads(snapshots(tmp_path)[0].read_text())["details"][0]
    assert detail["name"] == "Alpha"
    assert detail["display_name"] == "Synthetic long display name"


def test_genshin_targets_are_excluded_and_sync_marker_required():
    raw = gs_detail()
    value = normalize_genshin(raw)
    assert value["level"] == 90 and value["weapon"]["level"] == 20
    assert [s["displayed_level"] for s in value["skills"]] == [1, 7, 8]
    assert "99" not in json.dumps(value)
    raw["synced"] = False
    with pytest.raises(SourceDataInvalid):
        normalize_genshin(raw)


def test_genshin_explicit_empty_weapon_is_null_and_missing_fields_still_fail():
    raw = gs_detail()
    raw["weapon"] = {"name": "点击添加", "levels": []}
    assert normalize_genshin(raw)["weapon"] is None
    raw["weapon"]["name"] = "Synthetic weapon"
    with pytest.raises(SourceDataInvalid):
        normalize_genshin(raw)


@pytest.mark.parametrize(
    "value",
    [
        {"rows": [{"name": "A", "level": "Lv.1"}] * 2},
        {"rows": [{"name": "A", "level": "-"}]},
        {"rows": []},
    ],
)
def test_page_roster_rejects_duplicates_unowned_and_empty(value):
    with pytest.raises(SourceDataInvalid):
        roster(value)


def test_zzz_recommendations_cannot_enter_current_fields():
    raw = {
        "name": "A",
        "display_name": "A",
        "current_weapon": "当前音擎\n音擎推荐\nSynthetic",
        "current_skills": "当前技能等级\n01",
        "current_discs": "当前装备驱动盘\n尚未装备驱动盘",
    }
    with pytest.raises(SourceDataInvalid):
        normalize_zzz(raw)


def test_zzz_only_explicit_empty_engine_can_become_null():
    raw = {
        "name": "A",
        "display_name": "A",
        "current_weapon": "EMPTY",
        "current_skills": "当前技能等级\n01",
        "current_discs": "当前装备驱动盘\n尚未装备驱动盘",
    }
    value = normalize_zzz(raw)
    assert value["current_weapon"] is None
    assert normalize_zzz(value, stored=True) == value
    with pytest.raises(SourceDataInvalid):
        normalize_zzz(raw, stored=True)
    raw["current_weapon"] = None
    with pytest.raises(SourceDataInvalid):
        normalize_zzz(raw)
    raw["current_weapon"] = "unknown"
    with pytest.raises(SourceDataInvalid):
        normalize_zzz(raw)


def test_atomic_page_save_failure_preserves_old_snapshot(tmp_path, monkeypatch):
    port = PagePort()
    assert cli.main(arguments(tmp_path, "--game", "genshin"), client=port.client()) == 0
    file = snapshots(tmp_path)[0]
    previous = file.read_bytes()
    snapshot = json.loads(previous)
    role = identity(snapshot["profile"], "genshin")
    store = PageSnapshotStore(
        direct_configuration("genshin", role.uid, storage_root=tmp_path / "results"),
        role,
        normalize_genshin,
    )
    changed = deepcopy(snapshot)
    changed["details"][0]["weapon"]["level"] += 1

    def fail(*args):
        raise OSError("synthetic failure before replace")

    monkeypatch.setattr("hoyo_roster.page_snapshot.atomic_write_metadata", fail)
    with store.locked(), pytest.raises(OSError):
        store.save(changed)
    assert file.read_bytes() == previous


def test_page_store_rejects_rollback_and_is_noop_for_same_data(tmp_path):
    port = PagePort()
    assert cli.main(arguments(tmp_path, "--game", "genshin"), client=port.client()) == 0
    file = snapshots(tmp_path)[0]
    value = json.loads(file.read_text())
    role = identity(value["profile"], "genshin")
    store = PageSnapshotStore(
        direct_configuration("genshin", role.uid, storage_root=tmp_path / "results"),
        role,
        normalize_genshin,
    )
    with store.locked():
        assert store.save(value) is False
        value["roster"][0]["level"] -= 1
        with pytest.raises(SourceDataInvalid):
            store.save(value)


def test_official_unsupported_preserves_owned_row_without_inventing_detail(tmp_path, capsys):
    port = PagePort()
    port.unsupported = True
    assert cli.main(arguments(tmp_path, "--game", "genshin"), client=port.client()) == 0
    value = json.loads(snapshots(tmp_path)[0].read_text())
    assert len(value["roster"]) == len(value["details"]) == 2
    unavailable = next(d for d in value["details"] if d.get("status") == "unsupported")
    assert set(unavailable) == {"name", "status", "reason"}
    assert "PAGE_DETAILS_UNAVAILABLE=1" in capsys.readouterr().out
    unavailable["name"] = "Alpha"
    with pytest.raises(SourceDataInvalid):
        normalize_genshin(unavailable)


def test_midgame_recovery_discards_both_uncommitted_passes(tmp_path):
    port = PagePort()
    port.fail_at[("genshin", "page:detail", 1)] = "AUTHENTICATION_REQUIRED"
    assert cli.main(arguments(tmp_path), client=port.client()) == 0
    assert port.counts[("genshin", "page:roster")] == 3
    assert port.counts[("zzz", "page:roster")] == 2
    assert sum(c["method"] == "login.confirm" for c in port.calls) == 1
    assert len(snapshots(tmp_path)) == 2


def test_page_login_cancel_never_saves(tmp_path):
    port = PagePort(authenticated=False)
    port.answers = ["cancel"]
    assert cli.main(arguments(tmp_path), client=port.client()) == 1
    assert not snapshots(tmp_path)


def test_page_post_replace_durability_error_retains_valid_new_file(tmp_path, monkeypatch):
    from hoyo_roster import platform_io as io
    from hoyo_roster.storage.common import AtomicWriteError

    assert cli.main(arguments(tmp_path, "--game", "genshin"), client=PagePort().client()) == 0
    file = snapshots(tmp_path)[0]
    value = json.loads(file.read_text())
    role = identity(value["profile"], "genshin")
    store = PageSnapshotStore(
        direct_configuration("genshin", role.uid, storage_root=tmp_path / "results"),
        role,
        normalize_genshin,
    )
    value["details"][0]["weapon"]["level"] += 1
    real_sync = io.sync_directory

    def fail_after_replace(path):
        if path == store.root:
            raise OSError("synthetic directory persistence failure")
        real_sync(path)

    monkeypatch.setattr(io, "sync_directory", fail_after_replace)
    with store.locked(), pytest.raises(AtomicWriteError):
        store.save(value)
    assert json.loads(file.read_text()) == value


def test_page_store_rejects_redirected_current_without_touching_target(tmp_path):
    from hoyo_roster import platform_io as io

    assert cli.main(arguments(tmp_path, "--game", "genshin"), client=PagePort().client()) == 0
    file = snapshots(tmp_path)[0]
    value = json.loads(file.read_text())
    role = identity(value["profile"], "genshin")
    store = PageSnapshotStore(
        direct_configuration("genshin", role.uid, storage_root=tmp_path / "results"),
        role,
        normalize_genshin,
    )
    file.unlink()
    target = tmp_path / "untouched"
    file.symlink_to(target)
    with store.locked(), pytest.raises(io.UnsafePath):
        store.save(value)
    assert file.is_symlink() and not target.exists()


def test_one_game_storage_failure_continues_next_game(tmp_path, monkeypatch, capsys):
    real_save = PageSnapshotStore.save

    def fail_genshin(self, value):
        if self.role.game == "genshin":
            raise OSError("synthetic precommit failure")
        return real_save(self, value)

    monkeypatch.setattr(PageSnapshotStore, "save", fail_genshin)
    assert cli.main(arguments(tmp_path), client=PagePort().client()) == 1
    assert [json.loads(p.read_text())["game"] for p in snapshots(tmp_path)] == ["zzz"]
    output = capsys.readouterr().out
    assert "PAGE_SAVE_FAILED" in output and "GAME=zzz\nGAME_STATUS=PASS" in output


def test_page_durability_failure_reports_unknown_without_claiming_saved(
    tmp_path, monkeypatch, capsys
):
    from hoyo_roster.storage.common import AtomicWriteError

    def fail(self, value):
        raise AtomicWriteError("synthetic post-replace failure")

    monkeypatch.setattr(PageSnapshotStore, "save", fail)
    assert cli.main(arguments(tmp_path, "--game", "genshin"), client=PagePort().client()) == 1
    output = capsys.readouterr().out
    assert "COMMIT_STATE=DURABILITY_UNKNOWN" in output
    assert "ROLE_STATUS=SAVED" not in output
