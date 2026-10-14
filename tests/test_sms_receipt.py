"""Only acknowledgement metadata is inspected; no real phone/SMS/service is used."""

import asyncio
from types import SimpleNamespace

import pytest

from hoyo_roster.errors import RateLimited, RiskControl, SmsReceiptUnknown, SmsRequestRejected
from hoyo_roster.sms import SMS_PATH, SmsReceiptWatch, sms_request
from tests.test_login_flow import Context


def request(url="https://passport-api.mihoyo.com" + SMS_PATH, method="POST"):
    return SimpleNamespace(url=url, method=method)


@pytest.mark.parametrize(
    "url,method",
    [
        ("http://passport-api.mihoyo.com" + SMS_PATH, "POST"),
        ("https://passport-api.mihoyo.com.evil.invalid" + SMS_PATH, "POST"),
        ("https://passport-api.mihoyo.com:444" + SMS_PATH, "POST"),
        ("https://user:pass@passport-api.mihoyo.com" + SMS_PATH, "POST"),
        ("https://passport-api.mihoyo.com/other", "POST"),
        ("https://passport-api.mihoyo.com" + SMS_PATH, "GET"),
    ],
)
def test_unrelated_or_untrusted_request_is_not_receipt(url, method):
    assert not sms_request(request(url, method))


@pytest.mark.parametrize(
    "payload,status,error,label",
    [
        ({"retcode": 0, "data": {"sent_new": True, "countdown": 60}}, 200, None, "SMS_ACCEPTED"),
        ({"retcode": 0, "data": {"sent_new": False, "countdown": 59}}, 200, None, "SMS_COOLDOWN"),
        ({"retcode": 0}, 200, SmsReceiptUnknown, None),
        ({"retcode": 0, "data": {"sent_new": 1, "countdown": 60}}, 200, SmsReceiptUnknown, None),
        (
            {"retcode": 0, "data": {"sent_new": True, "countdown": True}},
            200,
            SmsReceiptUnknown,
            None,
        ),
        ({"retcode": 0, "data": {"sent_new": True, "countdown": -1}}, 200, SmsReceiptUnknown, None),
        ({"retcode": True}, 200, SmsReceiptUnknown, None),
        ({"retcode": 1034}, 200, RiskControl, None),
        ({"retcode": 12345}, 200, SmsRequestRejected, None),
        ({"retcode": 0}, 503, SmsReceiptUnknown, None),
        ({"retcode": 0}, 429, RateLimited, None),
    ],
)
def test_strict_receipt_and_listener_cleanup(payload, status, error, label, capsys):
    async def run():
        context = Context([])
        watch = SmsReceiptWatch(context)
        watch.start()
        req = request()
        for callback in context.callbacks["request"]:
            callback(req)

        async def json():
            return payload

        response = SimpleNamespace(request=req, status=status, json=json)
        for callback in context.callbacks["response"]:
            callback(response)
        await watch.task
        if error:
            with pytest.raises(error):
                watch.check()
        else:
            watch.check()
            assert watch.state == label
        await watch.close()
        assert all(not callbacks for callbacks in context.callbacks.values())
        assert watch.request is None

    asyncio.run(run())
    output = capsys.readouterr().out
    assert "SMS_SENT" not in output
    if label:
        assert f"PROGRESS={label}" in output
    else:
        assert "SMS_ACCEPTED" not in output and "SMS_COOLDOWN" not in output


def test_receipt_unknown_no_retry_and_cancel_drains(monkeypatch):
    from hoyo_roster import sms

    clock = [0.0]
    monkeypatch.setattr(sms, "monotonic", lambda: clock[0])

    async def run():
        context = Context([])
        watch = SmsReceiptWatch(context)
        watch.start()
        watch._request(request())
        clock[0] = 30
        with pytest.raises(SmsReceiptUnknown):
            watch.check()

        async def json():
            await asyncio.Future()

        watch._response(SimpleNamespace(request=watch.request, status=200, json=json))
        await asyncio.sleep(0)
        await watch.close()
        assert watch.task.done() and watch.task.cancelled()
        assert all(not values for values in context.callbacks.values())

    asyncio.run(run())


def test_request_failure_and_repeat_never_confirm(capsys):
    async def run():
        context = Context([])
        watch = SmsReceiptWatch(context)
        watch.start()
        req = request()
        watch._request(req)
        watch._failed(req)
        with pytest.raises(SmsReceiptUnknown):
            watch.check()
        watch._request(request())
        with pytest.raises(SmsRequestRejected):
            watch.check()
        await watch.close()

    asyncio.run(run())
    assert "SMS_ACCEPTED" not in capsys.readouterr().out
