"""Donor service initialization over the current session, without login coupling."""

import pytest

from hoyo_roster import cli
from tests.fixtures import FakePort
from tests.test_client_workflow import arguments, no_source_delay  # noqa: F401


def test_zzz_initializes_once_for_both_passes(tmp_path):
    port = FakePort()
    assert cli.main(arguments(tmp_path), client=port.client()) == 0
    assert port.counts[("zzz", "cultivate:badge-login")] == 1
    assert port.counts[("zzz", "cultivate:user/avatar_basic_list")] == 2
    assert not any(call["method"] == "login.open" for call in port.calls)
    assert all(call["session"] == port.ref for call in port.calls[1:])


def test_zzz_service_expiry_refreshes_service_without_user_login(tmp_path):
    port = FakePort()
    port.fail_at[("zzz", "cultivate:user/avatar_basic_list", 2)] = "AUTHENTICATION_REQUIRED"
    assert cli.main(arguments(tmp_path), client=port.client()) == 0
    assert port.counts[("zzz", "cultivate:badge-login")] == 2
    assert port.counts[("zzz", "cultivate:user/avatar_basic_list")] == 3
    assert port.counts[("genshin", "cultivation-avatar-list")] == 2
    assert not any(call["method"] == "login.open" for call in port.calls)


def test_repeated_service_expiry_is_bounded(tmp_path, capsys):
    port = FakePort()
    for number in range(1, 5):
        port.fail_at[("zzz", "cultivate:user/avatar_basic_list", number)] = (
            "AUTHENTICATION_REQUIRED"
        )
    assert cli.main(arguments(tmp_path, "--game", "zzz"), client=port.client()) == 1
    assert port.counts[("zzz", "cultivate:user/avatar_basic_list")] == 4
    assert sum(call["method"] == "login.confirm" for call in port.calls) == 1
    assert "TASK_STATUS=PASS" not in capsys.readouterr().out


@pytest.mark.parametrize("code", ["RISK_CONTROL", "RATE_LIMITED"])
def test_service_risk_or_rate_limit_stops_without_login(tmp_path, code):
    port = FakePort()
    port.fail_at[("zzz", "cultivate:badge-login", 1)] = code
    assert cli.main(arguments(tmp_path), client=port.client()) == 1
    assert port.counts[("zzz", "cultivate:user/avatar_basic_list")] == 0
    assert not any(call["method"] == "login.open" for call in port.calls)


@pytest.mark.parametrize("mutation", ["origin", "ready", "extra"])
def test_invalid_service_receipt_cannot_continue_collection(tmp_path, mutation):
    class BadReceipt(FakePort):
        async def exchange(self, message):
            result = await super().exchange(message)
            if message["method"] == "service.prepare":
                receipt = result["result"]
                if mutation == "origin":
                    receipt["origin"]["url"] = "https://example.invalid/receipt"
                elif mutation == "ready":
                    receipt["ready"] = 1
                else:
                    receipt["authentication"] = "synthetic-private-value"
            return result

    port = BadReceipt()
    assert cli.main(arguments(tmp_path, "--game", "zzz"), client=port.client()) == 1
    assert port.counts[("zzz", "cultivate:user/avatar_basic_list")] == 0
