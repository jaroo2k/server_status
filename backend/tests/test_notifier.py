import asyncio
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.notifier.alerts import MAX_MESSAGE_CHARS, notification_text, should_alert
from app.notifier.dispatcher import dispatch_forever
from app.notifier.telegram import TelegramClient, TelegramError, TelegramRetryAfter

TOKEN = "123456:SECRET-TOKEN"
T = datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc)


def finding(severity="critical", title="디스크 오류", message="I/O error on sda", source="journal"):
    return SimpleNamespace(severity=severity, title=title, message=message, source=source)


def text(findings=(), previous="ok", current="ok", error=None):
    return notification_text("web-1", list(findings), previous, current, error, T)


# ---------- which findings alert ----------
@pytest.mark.parametrize(
    "severity, previous_ack, minimum, expected",
    [
        ("critical", None, "critical", True),  # new event
        ("critical", True, "critical", True),  # recurred after being acknowledged
        ("critical", False, "critical", False),  # repeat of an open event
        ("warning", None, "critical", False),  # below the threshold
        ("warning", None, "warning", True),
        ("critical", None, "warning", True),
        ("info", None, "warning", False),
    ],
)
def test_should_alert(severity, previous_ack, minimum, expected):
    assert should_alert(severity, previous_ack, minimum) is expected


# ---------- message contents ----------
def test_nothing_to_report_produces_no_message():
    assert text() is None


def test_message_names_server_time_in_kst_and_finding():
    message = text([finding()])

    assert "web-1" in message
    assert "2026-11-02 14:30 KST" in message
    assert "디스크 오류" in message
    assert "I/O error on sda" in message


def test_entering_unreachable_reports_the_reason():
    message = text(previous="ok", current="unreachable", error="TimeoutError: timed out")

    assert "접속 불가" in message
    assert "TimeoutError: timed out" in message


def test_staying_unreachable_is_silent():
    assert text(previous="unreachable", current="unreachable", error="x") is None


@pytest.mark.parametrize("current", ["ok", "warning", "critical", "degraded"])
def test_leaving_unreachable_reports_recovery(current):
    assert "복구" in text(previous="unreachable", current=current)


@pytest.mark.parametrize(
    "previous, current", [("ok", "critical"), ("ok", "warning"), ("ok", "degraded"), ("unknown", "ok")]
)
def test_other_status_changes_are_silent(previous, current):
    assert text(previous=previous, current=current) is None


def test_status_change_is_listed_before_findings():
    message = text([finding()], previous="ok", current="unreachable", error="boom")

    assert message.index("접속 불가") < message.index("디스크 오류")


def test_many_findings_are_capped_with_remaining_count():
    message = text([finding(title=f"이벤트-{i:02d}") for i in range(13)])

    assert "이벤트-09" in message
    assert "이벤트-10" not in message
    assert "외 3건" in message


def test_huge_log_lines_stay_within_telegram_limit():
    findings = [finding(title=f"이벤트-{i}", message="x" * 100_000) for i in range(10)]

    assert len(text(findings)) <= MAX_MESSAGE_CHARS
    assert MAX_MESSAGE_CHARS <= 4096


def test_huge_status_error_stays_within_telegram_limit():
    message = text(previous="ok", current="unreachable", error="y" * 100_000)

    assert len(message) <= MAX_MESSAGE_CHARS


# ---------- settings ----------
@pytest.mark.parametrize(
    "token, chat, enabled",
    [("t", "1", True), ("t", "", False), ("", "1", False), ("", "", False)],
)
def test_alerts_are_enabled_only_with_token_and_chat(token, chat, enabled):
    settings = Settings(_env_file=None, telegram_bot_token=token, telegram_chat_id=chat)

    assert settings.telegram_enabled is enabled


def test_invalid_min_severity_is_rejected_at_startup():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, telegram_min_severity="urgent")


# ---------- telegram client against a real local HTTP server ----------
@pytest.fixture
def telegram_api():
    api = SimpleNamespace(requests=[], status=200, body={"ok": True, "result": {}})

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            api.requests.append((self.path, json.loads(raw)))
            payload = api.body if isinstance(api.body, bytes) else json.dumps(api.body).encode()
            self.send_response(api.status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api.base = f"http://127.0.0.1:{server.server_port}"
    yield api
    server.shutdown()
    server.server_close()
    thread.join()


def send(api_base, message="hello"):
    client = TelegramClient(TOKEN, "-100200", api_base=api_base)
    asyncio.run(client.send(message))


def test_send_posts_plain_text_to_the_chat(telegram_api):
    send(telegram_api.base, "경고 <b> & _text_")

    path, body = telegram_api.requests[0]
    assert path == f"/bot{TOKEN}/sendMessage"
    assert body["chat_id"] == "-100200"
    assert body["text"] == "경고 <b> & _text_"
    assert "parse_mode" not in body  # log text must never be read as markup


def test_rate_limit_exposes_retry_after(telegram_api):
    telegram_api.status = 429
    telegram_api.body = {"ok": False, "parameters": {"retry_after": 37}}

    with pytest.raises(TelegramRetryAfter) as e:
        send(telegram_api.base)

    assert e.value.retry_after == 37


def test_api_error_includes_description(telegram_api):
    telegram_api.status = 400
    telegram_api.body = {"ok": False, "description": "Bad Request: chat not found"}

    with pytest.raises(TelegramError, match="chat not found"):
        send(telegram_api.base)


def test_non_json_error_response_is_a_telegram_error(telegram_api):
    telegram_api.status = 502
    telegram_api.body = b"<html>bad gateway</html>"

    with pytest.raises(TelegramError, match="502"):
        send(telegram_api.base)


def test_transport_error_does_not_leak_the_token():
    # urllib puts the whole URL, token included, in this exception's message
    with pytest.raises(TelegramError) as e:
        send("not-a-url")

    assert TOKEN not in str(e.value)
    assert e.value.__cause__ is None
    assert e.value.__suppress_context__


# ---------- dispatcher loop ----------
class Stop(Exception):
    pass


def run_dispatcher(outcomes):
    """Feeds outcomes to the loop, returning each wait it chose."""
    remaining = iter(outcomes)
    delays = []

    async def once():
        outcome = next(remaining)
        if outcome:
            raise outcome

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == len(outcomes):
            raise Stop

    with pytest.raises(Stop):
        asyncio.run(dispatch_forever(once, sleep=sleep))
    return delays


def test_dispatcher_backs_off_on_failure_and_resets_on_success():
    error = TelegramError("down")

    assert run_dispatcher([None, error, error, None, error]) == [5, 10, 20, 5, 10]


def test_dispatcher_backoff_is_capped():
    assert run_dispatcher([TelegramError("down")] * 8)[-1] == 300


def test_dispatcher_waits_as_long_as_telegram_asks():
    assert run_dispatcher([TelegramRetryAfter(37)]) == [37]


def test_dispatcher_survives_unexpected_errors():
    assert run_dispatcher([RuntimeError("db gone"), None]) == [10, 5]
