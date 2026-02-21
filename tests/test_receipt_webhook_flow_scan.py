from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import app
import db
import notifications
import sms_provider


class _DummyHandler:
    _trailer_label = app.Handler._trailer_label
    _booking_period_label = app.Handler._booking_period_label
    _send_paid_sms_notifications = app.Handler._send_paid_sms_notifications


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_database.db")
    db.init_db()


@pytest.fixture()
def dummy_handler() -> _DummyHandler:
    return _DummyHandler()


@pytest.fixture()
def disable_sms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sms_provider, "get_admin_sms_number_e164", lambda: None)
    monkeypatch.setattr(sms_provider, "send_sms", lambda *_args, **_kwargs: False)


def _create_paid_booking(*, receipt_requested: bool, customer_email: str | None) -> int:
    start_dt = datetime(2026, 7, 1, 10, 0)
    end_dt = start_dt + timedelta(hours=2)
    booking_id, _ = db.create_booking(
        "KAP",
        "TWO_HOURS",
        start_dt,
        end_dt,
        customer_phone_temp=None,
        customer_email_temp=customer_email,
        receipt_requested_temp=receipt_requested,
    )
    db.set_swish_status(booking_id, "PAID", booking_status="CONFIRMED")
    return booking_id


def _set_booking_reference(booking_id: int, booking_reference: str) -> None:
    conn = sqlite3.connect(db.DB_PATH)
    try:
        conn.execute("UPDATE bookings SET booking_reference = ? WHERE id = ?", (booking_reference, booking_id))
        conn.commit()
    finally:
        conn.close()


def _booking_payload() -> dict[str, Any]:
    return {
        "id": 123,
        "booking_reference": "DHS-20260221-000123",
        "trailer_type": "KAP",
        "start_dt": "2026-02-21T10:00",
        "end_dt": "2026-02-21T12:00",
        "price": 200,
        "customer_email_temp": "receipt@example.com",
        "receipt_requested_temp": 1,
    }


def test_a_paid_requested_with_email_and_env_posts_once_with_expected_payload(
    isolated_db: None,
    dummy_handler: _DummyHandler,
    disable_sms: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/webhook")
    monkeypatch.setenv("NOTIFY_WEBHOOK_SECRET", "secret-1")
    calls: list[dict[str, Any]] = []

    def _fake_post(url: str, json: dict[str, Any], timeout: int, allow_redirects: bool) -> _FakeResponse:
        calls.append({"url": url, "json": json, "timeout": timeout, "allow_redirects": allow_redirects})
        return _FakeResponse(302, "redirect", {"Location": "https://example.com/final"})

    monkeypatch.setattr(notifications.requests, "post", _fake_post)

    booking_id = _create_paid_booking(receipt_requested=True, customer_email="receipt@example.com")
    dummy_handler._send_paid_sms_notifications(booking_id)
    dummy_handler._send_paid_sms_notifications(booking_id)

    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://example.com/webhook"
    assert call["timeout"] == 10
    assert call["allow_redirects"] is False

    payload = call["json"]
    assert payload["event"] == "booking.confirmed"
    assert payload["companyName"] == "Dalsjöfors Hyrservice AB"
    assert payload["organizationNumber"] == "559062-4556"
    assert payload["receiptRequested"] is True
    assert payload["customerEmail"] == "receipt@example.com"
    assert payload["swishStatus"] == "PAID"
    assert payload["secret"] == "secret-1"


def test_b_receipt_not_requested_never_posts(
    isolated_db: None,
    dummy_handler: _DummyHandler,
    disable_sms: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/webhook")
    called = False

    def _fake_post(*_args: Any, **_kwargs: Any) -> _FakeResponse:
        nonlocal called
        called = True
        return _FakeResponse(302, "redirect")

    monkeypatch.setattr(notifications.requests, "post", _fake_post)

    booking_id = _create_paid_booking(receipt_requested=False, customer_email="receipt@example.com")
    dummy_handler._send_paid_sms_notifications(booking_id)

    assert called is False


def test_c_email_missing_never_posts(
    isolated_db: None,
    dummy_handler: _DummyHandler,
    disable_sms: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/webhook")
    called = False

    def _fake_post(*_args: Any, **_kwargs: Any) -> _FakeResponse:
        nonlocal called
        called = True
        return _FakeResponse(302, "redirect")

    monkeypatch.setattr(notifications.requests, "post", _fake_post)

    booking_id = _create_paid_booking(receipt_requested=True, customer_email=None)
    dummy_handler._send_paid_sms_notifications(booking_id)

    assert called is False


def test_d_missing_notify_webhook_url_returns_false_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("NOTIFY_WEBHOOK_URL", raising=False)

    def _fail_if_called(*_args: Any, **_kwargs: Any) -> _FakeResponse:
        raise AssertionError("requests.post must not be called")

    monkeypatch.setattr(notifications.requests, "post", _fail_if_called)
    caplog.set_level("INFO")

    ok = notifications.send_receipt_webhook(_booking_payload())

    assert ok is False
    assert "WEBHOOK_DISABLED" in caplog.text


def test_e_webhook_303_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/webhook")
    monkeypatch.setattr(notifications.requests, "post", lambda *_a, **_k: _FakeResponse(303, "redirect"))

    assert notifications.send_receipt_webhook(_booking_payload()) is True


def test_f_webhook_200_with_ok_body_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/webhook")
    monkeypatch.setattr(notifications.requests, "post", lambda *_a, **_k: _FakeResponse(200, "All OK"))

    assert notifications.send_receipt_webhook(_booking_payload()) is True


def test_g_webhook_200_without_ok_body_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/webhook")
    monkeypatch.setattr(notifications.requests, "post", lambda *_a, **_k: _FakeResponse(200, "accepted"))

    assert notifications.send_receipt_webhook(_booking_payload()) is True


def test_h_webhook_exception_returns_false_without_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/webhook")

    def _raise_timeout(*_args: Any, **_kwargs: Any) -> _FakeResponse:
        raise notifications.requests.Timeout("timeout")

    monkeypatch.setattr(notifications.requests, "post", _raise_timeout)

    assert notifications.send_receipt_webhook(_booking_payload()) is False


def test_i_test_booking_reference_skips_receipt_webhook(
    isolated_db: None,
    dummy_handler: _DummyHandler,
    disable_sms: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/webhook")
    called = False

    def _fake_post(*_args: Any, **_kwargs: Any) -> _FakeResponse:
        nonlocal called
        called = True
        return _FakeResponse(302, "redirect")

    monkeypatch.setattr(notifications.requests, "post", _fake_post)

    booking_id = _create_paid_booking(receipt_requested=True, customer_email="receipt@example.com")
    _set_booking_reference(booking_id, "TEST-20260221-000001")

    dummy_handler._send_paid_sms_notifications(booking_id)

    assert called is False


def test_j_webhook_200_with_json_success_true_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/webhook")
    monkeypatch.setattr(notifications.requests, "post", lambda *_a, **_k: _FakeResponse(200, '{"success": true}'))

    assert notifications.send_receipt_webhook(_booking_payload()) is True


def test_k_webhook_204_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/webhook")
    monkeypatch.setattr(notifications.requests, "post", lambda *_a, **_k: _FakeResponse(204, ""))

    assert notifications.send_receipt_webhook(_booking_payload()) is True


def test_l_webhook_200_with_json_ok_true_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/webhook")
    monkeypatch.setattr(notifications.requests, "post", lambda *_a, **_k: _FakeResponse(200, '{"ok": true}'))

    assert notifications.send_receipt_webhook(_booking_payload()) is True


def test_m_retry_timeout_and_5xx_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/webhook")
    calls = {"count": 0}
    sleeps: list[float] = []

    def _fake_post(*_args: Any, **_kwargs: Any) -> _FakeResponse:
        calls["count"] += 1
        if calls["count"] == 1:
            raise notifications.requests.Timeout("timeout")
        if calls["count"] == 2:
            return _FakeResponse(500, "temporary error")
        return _FakeResponse(200, "ok")

    monkeypatch.setattr(notifications.requests, "post", _fake_post)
    monkeypatch.setattr(notifications.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert notifications.send_receipt_webhook(_booking_payload()) is True
    assert calls["count"] == 3
    assert sleeps == [0.5, 1.0]


def test_n_no_retry_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/webhook")
    calls = {"count": 0}
    sleeps: list[float] = []

    def _fake_post(*_args: Any, **_kwargs: Any) -> _FakeResponse:
        calls["count"] += 1
        return _FakeResponse(400, "bad request")

    monkeypatch.setattr(notifications.requests, "post", _fake_post)
    monkeypatch.setattr(notifications.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert notifications.send_receipt_webhook(_booking_payload()) is False
    assert calls["count"] == 1
    assert sleeps == []


def test_o_idempotency_fail_does_not_block_next_trigger(
    isolated_db: None,
    dummy_handler: _DummyHandler,
    disable_sms: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/webhook")
    calls = {"count": 0}

    def _fake_post(*_args: Any, **_kwargs: Any) -> _FakeResponse:
        calls["count"] += 1
        if calls["count"] <= 3:
            return _FakeResponse(500, "temporary error")
        return _FakeResponse(200, "ok")

    monkeypatch.setattr(notifications.requests, "post", _fake_post)
    monkeypatch.setattr(notifications.time, "sleep", lambda _seconds: None)

    booking_id = _create_paid_booking(receipt_requested=True, customer_email="receipt@example.com")
    dummy_handler._send_paid_sms_notifications(booking_id)
    dummy_handler._send_paid_sms_notifications(booking_id)

    assert calls["count"] == 4
    booking = db.get_booking_by_id(booking_id)
    assert booking is not None
    assert booking.get("receipt_webhook_sent_at") is not None
    assert booking.get("receipt_webhook_lock_at") is None


def test_p_claim_receipt_webhook_send_allows_only_one_inflight(isolated_db: None) -> None:
    booking_id = _create_paid_booking(receipt_requested=True, customer_email="receipt@example.com")

    first = db.claim_receipt_webhook_send(booking_id)
    second = db.claim_receipt_webhook_send(booking_id)

    assert first is True
    assert second is False

    assert db.release_receipt_webhook_lock(booking_id) is True
    assert db.claim_receipt_webhook_send(booking_id) is True
