"""Audit regressions in disposable scopes; no live login or account data."""

import asyncio
import importlib.util
import subprocess
import sys
import threading
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from hoyo_roster import cli
from hoyo_roster.blocking import run_blocking
from hoyo_roster.jsonio import StrictJsonError, json_bytes, load_json
from hoyo_roster.storage.common import tree_digest
from hoyo_roster.user_workspace import LocalSnapshotStore, WorkspaceError, direct_configuration
from hoyo_roster.validation import ValidationFailure, run_validator
from tests import candidate_policy_for
from tests.fixtures import UID_A

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "instruction,expected",
    [
        ("Use --operation logout", "OBSOLETE_WORKFLOW_INSTRUCTION"),
        ("Use --operation invented-command", "UNDOCUMENTED_OPERATION"),
        ('Call host.answerLogin("completed")', "UNSCOPED_LOGIN_ANSWER"),
        ("关闭本工具和确切浏览器", "OBSOLETE_BROWSER_OWNERSHIP"),
    ],
)
def test_workflow_guard_rejects_stale_public_instructions(instruction, expected):
    spec = importlib.util.spec_from_file_location(
        "workflow_guard", ROOT / "tools/check_workflow.py"
    )
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    operations = set(cli.build_parser()._option_string_actions["--operation"].choices)
    assert any(
        issue.startswith(expected + ":")
        for issue in guard.document_problems(instruction, "synthetic.md", operations)
    )
    assert not guard.document_problems(
        "浏览器管理 Cookie/token，不导出 Profile。退出登录（logout）由用户在官方页面操作。"
        "--operation inspect。"
        'host.answerLogin("completed", question.attempt)',
        "synthetic.md",
        operations,
    )


def test_valid_prepare_changes_neither_generation_nor_bytes(tmp_path):
    store = LocalSnapshotStore(
        cli.CONTRACT_ROOT,
        direct_configuration("genshin", UID_A, storage_root=tmp_path / "results"),
        "genshin",
    )
    with store.locked():
        first = store.prepare()
        before = tree_digest(store.root)
        pointer_stat = store.current_file.stat()
        assert store.prepare() == first
        assert tree_digest(store.root) == before
        assert store.current_file.stat().st_mtime_ns == pointer_stat.st_mtime_ns
        store.transaction_file.write_text("synthetic invalid journal")
        with pytest.raises(WorkspaceError, match="RECOVERY_REQUIRED"):
            store.prepare()
        assert first.exists()


@pytest.mark.parametrize("operation", ("sync", "login"))
def test_hsr_paused_before_paths_browser_or_collection(tmp_path, monkeypatch, capsys, operation):
    def forbidden(*args, **kwargs):
        pytest.fail("paused operation reached workspace")

    monkeypatch.setattr(cli, "LocalSnapshotStore", forbidden)
    assert cli.main(["--game", "hsr", "--uid", UID_A, "--operation", operation]) == 1
    assert "ERROR_CODE=GAME_PAUSED" in capsys.readouterr().out


def test_hsr_decimal_offline_commit_inspect_recover(tmp_path):
    from hoyo_roster.games.hsr.candidate import create_candidate
    from hoyo_roster.games.hsr.model import Snapshot, SnapshotDiagnostics
    from hoyo_roster.games.hsr.normalize import normalize_profile

    expected = Decimal("0.051234567890123456789")
    store = LocalSnapshotStore(
        cli.CONTRACT_ROOT,
        direct_configuration("hsr", UID_A, storage_root=tmp_path / "results"),
        "hsr",
        candidate_policy=candidate_policy_for("hsr"),
    )
    with store.locked():
        workspace = store.prepare()
        snapshot = Snapshot(
            normalize_profile(
                {"uid": UID_A, "server": "prod_gf_cn", "nickname": "Synthetic", "player_level": 20}
            ),
            {
                "schema_version": "honkai-star-rail-roster/v1",
                "game": "honkai-star-rail",
                "characters": {"1001": {"name": "Synthetic", "level": 20}},
            },
            {
                "1001": {
                    "schema_version": "honkai-star-rail-build/v1",
                    "game": "honkai-star-rail",
                    "panel_context": "out_of_combat",
                    "panel": {"crit_rate": expected},
                }
            },
            SnapshotDiagnostics(0, 0, 0, 0, 0, 0, 0, (), (), 0),
            {},
        )
        item = create_candidate(workspace, snapshot, uid=UID_A, candidate_base=store.candidate_base)
        result = store.commit(item.game_dir)
        assert result.state == "COMMITTED"
        item.cleanup()
        current = store.current()
        assert store.inspect()["current"] == "PRESENT" and store.recover() == current
        build = current / "data/honkai-star-rail/builds/1001.json"
        assert load_json(build)["panel"]["crit_rate"] == expected
        spec = importlib.util.spec_from_file_location(
            "offline_validator", cli.CONTRACT_ROOT / "tools/validate_repository.py"
        )
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)
        documents, _, errors = validator._load_documents(current, scope_game="honkai-star-rail")
        assert (
            not errors
            and documents["data/honkai-star-rail/builds/1001.json"]["panel"]["crit_rate"]
            == expected
        )
        assert (
            validator.validate_repository(
                current, enforce_empty=False, scope_game="honkai-star-rail"
            )
            == []
        )
        build.write_bytes(
            build.read_bytes().replace(str(expected).encode(), (str(expected) + "0").encode())
        )
        assert any(
            "not canonical" in e
            for e in validator.validate_repository(
                current, enforce_empty=False, scope_game="honkai-star-rail"
            )
        )


@pytest.mark.parametrize(
    "value", (Decimal("1.2300"), Decimal("-0.000"), Decimal("1E-30"), Decimal("1E+30"))
)
def test_exact_encoding_ignores_decimal_context(value):
    with localcontext() as context:
        context.prec = 2
        data = json_bytes({"value": value})
    assert Decimal(__import__("json").loads(data, parse_float=Decimal)["value"]) == value


@pytest.mark.parametrize("value", (Decimal("NaN"), Decimal("Infinity"), float("nan")))
def test_nonfinite_encoding_rejected(value):
    with pytest.raises(StrictJsonError):
        json_bytes({"value": value})


@pytest.mark.parametrize("optimize", (False, True))
@pytest.mark.parametrize(
    "shape,code",
    (
        ("absent", "WHEEL_MISSING"),
        ("empty", "WHEEL_MISSING"),
        ("wheel-only", "SDIST_MISSING"),
        ("zero-wheel", "WHEEL_EMPTY"),
        ("corrupt", "ARCHIVE_INVALID"),
    ),
)
def test_distribution_requires_real_artifacts(tmp_path, optimize, shape, code):
    dist = tmp_path / "dist"
    if shape != "absent":
        dist.mkdir()
    if shape not in {"absent", "empty"}:
        (dist / "example.whl").write_bytes(b"" if shape == "zero-wheel" else b"invalid")
    if shape in {"zero-wheel", "corrupt"}:
        (dist / "example.tar.gz").write_bytes(b"invalid")
    result = subprocess.run(
        [
            sys.executable,
            *(["-O"] if optimize else []),
            str(ROOT / "tools/verify_distribution.py"),
            "--dist-dir",
            str(dist),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1 and f"ERROR_CODE={code}" in result.stdout
    assert "RESOURCES=PASS" not in result.stdout


def test_validator_timeout_is_fixed_failure(tmp_path, monkeypatch):
    from hoyo_roster.user_workspace import empty_workspace

    workspace = tmp_path / "workspace"
    empty_workspace(cli.CONTRACT_ROOT, workspace)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("synthetic-secret-command", 90)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ValidationFailure, match="VALIDATOR_TIMEOUT") as error:
        run_validator(workspace)
    assert "synthetic-secret" not in str(error.value)


def test_cancel_drains_write_and_cleanup_before_authority_release():
    entered, finish, cleaned = threading.Event(), threading.Event(), threading.Event()
    order = []

    def write():
        entered.set()
        finish.wait(5)
        order.append("write")
        return object()

    def cleanup(item):
        order.append("cleanup")
        cleaned.set()

    async def exercise():
        task = asyncio.create_task(run_blocking(write, on_cancel=cleanup))
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done() and not cleaned.is_set()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        order.append("release")

    asyncio.run(exercise())
    assert order == ["write", "cleanup", "release"]


@pytest.mark.parametrize("optimize", (False, True))
@pytest.mark.parametrize("empty", (None, "wheel", "sdist"))
def test_distribution_archive_positive_and_empty_controls(tmp_path, optimize, empty):
    import io
    import shutil
    import tarfile
    import zipfile

    (tmp_path / "tools").mkdir()
    (tmp_path / "src/hoyo_roster").mkdir(parents=True)
    (tmp_path / "third_party").mkdir()
    (tmp_path / "dist").mkdir()
    script = tmp_path / "tools/verify_distribution.py"
    shutil.copyfile(ROOT / "tools/verify_distribution.py", script)
    (tmp_path / "src/hoyo_roster/__init__.py").write_bytes(b"# synthetic public package\n")
    (tmp_path / "third_party/NOTICE").write_bytes(b"synthetic notice")
    wheel = tmp_path / "dist/synthetic.whl"
    sdist = tmp_path / "dist/synthetic.tar.gz"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("hoyo_roster/__init__.py", b"# synthetic public package\n")
        archive.writestr("synthetic.dist-info/licenses/LICENSE", b"synthetic license")
        archive.writestr("synthetic.dist-info/licenses/third_party/NOTICE", b"synthetic notice")
    resources = [
        "src/hoyo_roster/__init__.py",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "AGENTS.md",
        "CLAUDE.md",
        ".agents/skills/local-roster/SKILL.md",
        "docs/timed-collection.md",
        "tools/timed_collection.mjs",
        "tools/analyze_collection.py",
        "tools/verify_installation.py",
        "data/.gitkeep",
    ]
    with tarfile.open(sdist, "w:gz") as archive:
        for name in resources:
            content = b"# synthetic public package\n" if name.startswith("src/") else b""
            member = tarfile.TarInfo("synthetic/" + name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    if empty is not None:
        (wheel if empty == "wheel" else sdist).write_bytes(b"")
    result = subprocess.run(
        [sys.executable, *(["-O"] if optimize else []), str(script)], capture_output=True, text=True
    )
    if empty is None:
        assert (
            result.returncode == 0
            and "WHEEL_RESOURCES=PASS" in result.stdout
            and "SDIST_RESOURCES=PASS" in result.stdout
        )
    else:
        assert result.returncode == 1 and f"ERROR_CODE={empty.upper()}_EMPTY" in result.stdout
