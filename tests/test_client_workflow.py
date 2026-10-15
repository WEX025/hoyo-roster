"""The real bridge, login, game pipelines, normalizers and stores; sources alone are synthetic."""

import asyncio
from io import BytesIO

import pytest

from hoyo_roster import cli
from hoyo_roster.auth import SessionService
from hoyo_roster.client import ClientSession, StdioPort
from hoyo_roster.errors import ClientProtocolError, LoginCancelled
from hoyo_roster.login import login
from hoyo_roster.jsonio import load_json
from hoyo_roster.storage.common import tree_digest
from hoyo_roster.user_workspace import LocalSnapshotStore, direct_configuration
from tests import candidate_policy_for
from tests.fixtures import FakePort, UID_A


@pytest.fixture(autouse=True)
def no_source_delay(monkeypatch):
    from hoyo_roster.request_gate import RequestGate

    async def reserve(self, **kwargs):
        return 0

    monkeypatch.setattr(RequestGate, "reserve", reserve)


def arguments(tmp_path, *extra):
    return [
        "--storage-root",
        str(tmp_path / "results"),
        "--runtime-root",
        str(tmp_path / "runtime"),
        *extra,
    ]


def store_for(tmp_path, game):
    return LocalSnapshotStore(
        cli.CONTRACT_ROOT,
        direct_configuration(game, UID_A, storage_root=tmp_path / "results"),
        game,
        candidate_policy=candidate_policy_for(game),
    )


def test_default_both_games_use_one_host_session_and_login_once(tmp_path, capsys):
    port = FakePort(authenticated=False)
    assert cli.main(arguments(tmp_path), client=port.client()) == 0
    assert sum(c["method"] == "login.confirm" for c in port.calls) == 1
    assert {c["session"] for c in port.calls[1:]} == {port.ref}
    assert {c["params"]["game"] for c in port.calls if c["method"] == "source.read"} == {
        "genshin",
        "zzz",
    }
    for game in ("genshin", "zzz"):
        assert store_for(tmp_path, game).inspect()["current"] == "PRESENT"
    output = capsys.readouterr().out
    assert UID_A not in output
    assert output.index("LOGIN_COMPLETED") < output.index("COLLECTING_1")
    assert "TASK_STATUS=PASS" in output
    assert not list((tmp_path / "runtime").rglob("browser-profile"))
    zzz = store_for(tmp_path, "zzz").current() / "data/zenless-zone-zero/builds/1011.json"
    from decimal import Decimal

    assert load_json(zzz)["panel"]["crit_rate"] == Decimal("0.051234567890123456789")


def test_login_returns_same_live_client_without_identity_or_business_work(tmp_path):
    port = FakePort(authenticated=False)

    async def run():
        client = port.client()
        await client.attach()
        handoff = await login(client)
        assert handoff.session is client and handoff.user_confirmed

    asyncio.run(run())
    assert [c["method"] for c in port.calls] == ["session.attach", "login.open", "login.confirm"]
    assert not (tmp_path / "results").exists()


def test_waiting_then_cancel_never_reads_game_sources():
    port = FakePort()
    port.answers = ["waiting", "cancel"]

    async def run():
        client = port.client()
        await client.attach()
        with pytest.raises(LoginCancelled):
            await login(client)

    asyncio.run(run())
    assert [c["method"] for c in port.calls].count("login.open") == 1
    assert not any(c["method"] == "source.read" for c in port.calls)


@pytest.mark.parametrize("phase", ["discovery", "pass_two"])
def test_zzz_expiry_resumes_only_zzz_using_original_session(tmp_path, phase):
    port = FakePort()
    if phase == "discovery":
        port.fail_at[("zzz", "binding", 1)] = "AUTHENTICATION_REQUIRED"
    else:
        port.fail_at[("zzz", "record:avatar/basic", 2)] = "AUTHENTICATION_REQUIRED"
    assert cli.main(arguments(tmp_path), client=port.client()) == 0
    assert port.counts[("genshin", "cultivation-avatar-list")] == 2
    assert sum(c["method"] == "login.confirm" for c in port.calls) == 1
    assert {c["session"] for c in port.calls[1:]} == {port.ref}
    if phase == "pass_two":
        assert port.counts[("zzz", "record:avatar/basic")] == 4


def test_repeated_capture_auth_loss_is_bounded(tmp_path, capsys):
    port = FakePort()
    for i in (1, 2):
        port.fail_at[("genshin", "cultivation-avatar-list", i)] = "AUTHENTICATION_REQUIRED"
    assert cli.main(arguments(tmp_path), client=port.client()) == 1
    assert sum(c["method"] == "login.confirm" for c in port.calls) == 1
    assert port.counts[("genshin", "cultivation-avatar-list")] == 2
    assert not any(
        c["method"] == "source.read" and c["params"]["game"] == "zzz" for c in port.calls
    )
    assert "TASK_STATUS=PASS" not in capsys.readouterr().out


def test_reauth_account_change_stops_before_second_game_capture(tmp_path, capsys):
    port = FakePort()
    port.login_changes_account = True
    port.fail_at[("zzz", "binding", 1)] = "AUTHENTICATION_REQUIRED"
    assert cli.main(arguments(tmp_path), client=port.client()) == 1
    assert port.counts[("zzz", "record:avatar/basic")] == 0
    assert "ERROR_CODE=WRONG_ACCOUNT" in capsys.readouterr().out
    assert store_for(tmp_path, "genshin").current() is not None


@pytest.mark.parametrize("code", ["CLIENT_SOURCE_UNAVAILABLE", "CLIENT_REQUEST_FAILED"])
def test_ordinary_game_failure_continues_other_game(tmp_path, code):
    port = FakePort()
    port.fail_at[("genshin", "cultivation-avatar-list", 1)] = code
    assert cli.main(arguments(tmp_path), client=port.client()) == 1
    assert store_for(tmp_path, "zzz").inspect()["current"] == "PRESENT"


@pytest.mark.parametrize(
    "game,phase",
    [
        (game, phase)
        for game in ("genshin", "zzz")
        for phase in ("open", "confirm", "current_identity")
    ]
    + [("zzz", "pinned_identity")],
)
def test_any_recovery_failure_stops_subsequent_sources(tmp_path, capsys, game, phase):
    class FailedRecovery(FakePort):
        failed_at = None

        async def exchange(self, message):
            result = await super().exchange(message)
            method = message["method"]
            fail = method == {"open": "login.open", "confirm": "login.confirm"}.get(phase)
            if method == "source.read" and phase == "pinned_identity":
                # Verification of an already pinned game's identity after confirmation.
                fail = (
                    message["params"]["game"] == "genshin"
                    and self.counts[("genshin", "binding")] == 2
                )
            if fail:
                result.update(result=None, error="CLIENT_REQUEST_FAILED")
            if result["error"] == "CLIENT_REQUEST_FAILED":
                self.failed_at = len(self.calls)
            return result

    port = FailedRecovery()
    port.fail_at[(game, "binding", 1)] = "AUTHENTICATION_REQUIRED"
    if phase == "current_identity":
        port.fail_at[(game, "binding", 2)] = "CLIENT_REQUEST_FAILED"
    assert cli.main(arguments(tmp_path, "--operation", "discover"), client=port.client()) == 1
    assert port.failed_at is not None
    assert not port.calls[port.failed_at :]
    if game == "genshin":
        assert port.counts[("zzz", "binding")] == 0
    output = capsys.readouterr().out
    assert "ERROR_CODE=CLIENT_REQUEST_FAILED" in output
    assert "TASK_STATUS=PASS" not in output


@pytest.mark.parametrize("kind", ["workspace", "unexpected"])
def test_failed_recovery_does_not_publish_new_session_version(tmp_path, kind):
    from hoyo_roster.errors import OperationError, WorkspaceError
    from hoyo_roster.game_runtime import stops_account

    async def check():
        if kind == "workspace":
            raise WorkspaceError("RECOVERY_REQUIRED")
        raise RuntimeError("synthetic private detail")

    async def run():
        async with SessionService(FakePort().client(), tmp_path / "runtime") as session:
            with pytest.raises((OperationError, WorkspaceError)) as caught:
                await session.recover(verify_current=check)
            error = caught.value
            code = error.code if isinstance(error, OperationError) else str(error)
            assert stops_account(error, code)
            assert code == ("RECOVERY_REQUIRED" if kind == "workspace" else "AUTH_RECOVERY_FAILED")
            assert "synthetic private" not in str(error)
            assert session.version == 0 and session.handoff is None

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["discover", "login"])
def test_login_and_discovery_never_create_business_results(tmp_path, operation):
    port = FakePort()
    assert cli.main(arguments(tmp_path, "--operation", operation), client=port.client()) == 0
    assert not (tmp_path / "results").exists()


def test_unavailable_reader_stops_without_login_or_result(tmp_path, capsys):
    port = FakePort(source_reader=False)
    assert cli.main(arguments(tmp_path), client=port.client()) == 1
    assert "ERROR_CODE=CLIENT_SOURCE_UNAVAILABLE" in capsys.readouterr().out
    assert not (tmp_path / "results").exists()
    assert [c["method"] for c in port.calls] == ["session.attach"]


def test_no_host_fails_before_private_paths(tmp_path, capsys):
    assert cli.main(arguments(tmp_path)) == 1
    assert "ERROR_CODE=CLIENT_BROWSER_REQUIRED" in capsys.readouterr().out
    assert not (tmp_path / "runtime").exists() and not (tmp_path / "results").exists()


@pytest.mark.parametrize("operation", ["sync", "login", "discover"])
def test_hsr_paused_before_host_or_paths(tmp_path, operation, capsys):
    port = FakePort()
    assert (
        cli.main(
            arguments(tmp_path, "--game", "hsr", "--operation", operation), client=port.client()
        )
        == 1
    )
    assert not port.calls and not (tmp_path / "runtime").exists()
    assert "GAME_PAUSED" in capsys.readouterr().out


def test_drift_refuses_save_and_noop_retains_generation(tmp_path):
    port = FakePort()
    args = arguments(tmp_path, "--game", "genshin")
    assert cli.main(args, client=port.client()) == 0
    store = store_for(tmp_path, "genshin")
    before = store.current()
    digest = tree_digest(before)
    assert cli.main(args, client=FakePort().client()) == 0
    assert store.current() == before and tree_digest(before) == digest
    drifting = FakePort()
    drifting.drift = True
    assert cli.main(args, client=drifting.client()) == 1
    assert store.current() == before and tree_digest(before) == digest


@pytest.mark.parametrize("mutation", ["session", "id", "version", "error", "extra"])
def test_mismatched_or_malformed_handoff_is_rejected(mutation):
    class Bad(FakePort):
        async def exchange(self, message):
            result = await super().exchange(message)
            if message["method"] != "session.attach":
                if mutation == "error":
                    result["error"] = "unknown-private-input"
                elif mutation == "extra":
                    result["extra"] = True
                else:
                    result[mutation] = False
            return result

    async def run():
        client = Bad().client()
        await client.attach()
        with pytest.raises(ClientProtocolError):
            await client.call("login.open", {"url": "unused"})

    asyncio.run(run())


@pytest.mark.parametrize("raw", [b"", b"{}\n", b"{bad}\n", b"{}"])
def test_stdio_frames_reject_eof_and_invalid_envelopes(raw):
    async def run():
        with pytest.raises(ClientProtocolError):
            await ClientSession(StdioPort(BytesIO(raw), BytesIO())).attach()

    asyncio.run(run())


def test_same_runtime_metadata_mutex_blocks_concurrent_tasks(tmp_path):
    port_a, port_b = FakePort(), FakePort()

    async def run():
        async with SessionService(port_a.client(), tmp_path / "runtime"):
            from hoyo_roster.errors import WorkspaceError

            with pytest.raises(WorkspaceError, match="USER_WORKSPACE_BUSY"):
                async with SessionService(port_b.client(), tmp_path / "runtime"):
                    pass

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_host",
        "wrong_path",
        "wrong_method",
        "redirect",
        "status",
        "retcode_bool",
        "duplicate_json",
    ],
)
def test_invalid_source_proof_rejects_game_and_allows_other_game(tmp_path, mutation):
    class InvalidSource(FakePort):
        async def exchange(self, message):
            result = await super().exchange(message)
            if message["method"] == "source.read" and message["params"]["game"] == "genshin":
                record = result["result"]
                if mutation == "wrong_host":
                    record["origin"]["url"] = "https://example.invalid/source"
                elif mutation == "wrong_path":
                    record["origin"]["url"] += "/other"
                elif mutation == "wrong_method":
                    record["origin"]["method"] = "DELETE"
                elif mutation == "redirect":
                    record["origin"]["redirected"] = True
                elif mutation == "status":
                    record["origin"]["status"] = 500
                elif mutation == "retcode_bool":
                    record["json"] = '{"retcode":true,"data":{}}'
                else:
                    record["json"] = '{"retcode":0,"retcode":0,"data":{}}'
            return result

    port = InvalidSource()
    assert cli.main(arguments(tmp_path), client=port.client()) == 1
    assert port.counts[("zzz", "record:avatar/basic")] == 2
    assert not any(c["method"] == "login.open" for c in port.calls)


@pytest.mark.parametrize(
    "mutation", ["identity", "filter", "pagination_bool", "pagination_extra", "missing"]
)
def test_genshin_source_coverage_proof_is_required(tmp_path, mutation):
    class InvalidEvidence(FakePort):
        async def exchange(self, message):
            result = await super().exchange(message)
            if (
                message["method"] == "source.read"
                and message["params"]["source"] == "cultivation-avatar-list"
            ):
                evidence = result["result"]["evidence"]
                if mutation == "identity":
                    evidence["request"]["uid"] = "987654321"
                elif mutation == "filter":
                    evidence["request"]["element_attr_ids"] = [1]
                elif mutation == "pagination_bool":
                    evidence["pages_observed"] = [True]
                elif mutation == "pagination_extra":
                    evidence["pages_observed"] = [1, 2]
                else:
                    del evidence["request"]
            return result

    port = InvalidEvidence()
    assert cli.main(arguments(tmp_path, "--game", "genshin"), client=port.client()) == 1
    store = store_for(tmp_path, "genshin")
    from hoyo_roster.storage.common import load_metadata

    assert load_metadata(store.current() / "data/genshin-impact/roster.json")["characters"] == {}


def test_rate_limit_stops_account_without_relogin(tmp_path):
    port = FakePort()
    port.fail_at[("genshin", "binding", 1)] = "RATE_LIMITED"
    assert cli.main(arguments(tmp_path), client=port.client()) == 1
    assert not any(c["method"] == "login.open" for c in port.calls)
    assert not any(
        c["method"] == "source.read" and c["params"]["game"] == "zzz" for c in port.calls
    )


@pytest.mark.parametrize("operation", ["init", "inspect", "recover"])
def test_pipe_flag_never_converts_offline_maintenance_to_collection(tmp_path, operation, capsys):
    assert cli.main(arguments(tmp_path, "--client-stdio", "--operation", operation)) == 1
    assert "ERROR_CODE=ARGUMENTS_INVALID" in capsys.readouterr().out
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "results").exists()
