"""Needs real Postgres (ON CONFLICT, shard filter). Drops all tables, so it only runs
against a database named in TEST_DATABASE_URL whose name contains "test"."""

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.analyzer.store import upsert_findings
from app.collector import runner
from app.collector.sources.base import CollectResult
from app.core.config import settings
from app.core.db import Base
from app.models import Event, Notification, Server
from app.notifier.dispatcher import send_pending
from app.notifier.telegram import TelegramError

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

T = datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc)


def with_db(test):
    assert "test" in make_url(URL).database, "refusing to drop tables outside a test database"

    async def main():
        engine = create_async_engine(URL, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        try:
            await test(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    asyncio.run(main())


def finding(fingerprint="fp-1", severity="critical"):
    return SimpleNamespace(
        rule_id="r",
        title=f"제목-{fingerprint}",
        category="server_error",
        severity=severity,
        source="journal",
        message="boom",
        fingerprint=fingerprint,
        record=SimpleNamespace(ts=T),
    )


async def add_server(session, server_id=1, **fields):
    server = Server(
        id=server_id,
        name=f"web-{server_id}",
        host="h",
        username="u",
        auth_type="key",
        secret_enc="x",
        **fields,
    )
    session.add(server)
    await session.commit()
    return server


# ---------- upsert_findings reports what should alert ----------
def test_new_critical_event_alerts_once_until_acknowledged():
    async def test(sessions):
        async with sessions() as s:
            await add_server(s)
            assert [f.fingerprint for f in await upsert_findings(s, 1, [finding()])] == ["fp-1"]
            assert await upsert_findings(s, 1, [finding()]) == []

            await s.execute(update(Event).values(acknowledged=True))
            assert [f.fingerprint for f in await upsert_findings(s, 1, [finding()])] == ["fp-1"]
            assert await upsert_findings(s, 1, [finding()]) == []

            event = await s.scalar(select(Event))
            assert event.count == 4
            assert event.acknowledged is False

    with_db(test)


def test_duplicates_in_one_batch_alert_once():
    async def test(sessions):
        async with sessions() as s:
            await add_server(s)
            alerts = await upsert_findings(s, 1, [finding(), finding(), finding("fp-2")])

            assert [f.fingerprint for f in alerts] == ["fp-1", "fp-2"]

    with_db(test)


def test_warning_is_stored_without_alerting():
    async def test(sessions):
        async with sessions() as s:
            await add_server(s)

            assert await upsert_findings(s, 1, [finding(severity="warning")]) == []
            assert (await s.scalar(select(Event))).severity == "warning"

    with_db(test)


# ---------- send_pending ----------
async def add_notification(session, text, server_id=1):
    session.add(Notification(server_id=server_id, text=text))
    await session.commit()


async def no_wait(_):
    pass


def test_pending_are_sent_oldest_first_and_marked_sent():
    async def test(sessions):
        sent = []

        async def send(text):
            sent.append(text)

        async with sessions() as s:
            await add_server(s)
            await add_notification(s, "a")
            await add_notification(s, "b")
            await send_pending(s, send, worker_index=0, worker_count=1, sleep=no_wait)
            await send_pending(s, send, worker_index=0, worker_count=1, sleep=no_wait)

        assert sent == ["a", "b"]

    with_db(test)


def test_failed_send_stays_pending_without_resending_earlier_ones():
    async def test(sessions):
        sent = []
        down = True

        async def send(text):
            if down and text == "b":
                raise TelegramError("down")
            sent.append(text)

        async with sessions() as s:
            await add_server(s)
            for text in "abc":
                await add_notification(s, text)
            with pytest.raises(TelegramError):
                await send_pending(s, send, worker_index=0, worker_count=1, sleep=no_wait)

        async with sessions() as s:  # as after a collector restart
            assert sent == ["a"]
            down = False
            await send_pending(s, send, worker_index=0, worker_count=1, sleep=no_wait)

        assert sent == ["a", "b", "c"]

    with_db(test)


def test_each_worker_only_sends_its_own_shard():
    async def test(sessions):
        sent = []

        async def send(text):
            sent.append(text)

        async with sessions() as s:
            await add_server(s, 1)
            await add_server(s, 2)
            await add_notification(s, "one", server_id=1)
            await add_notification(s, "two", server_id=2)
            await send_pending(s, send, worker_index=0, worker_count=2, sleep=no_wait)

        assert sent == ["two"]

    with_db(test)


# ---------- wired through the collector ----------
class FakePool:
    def __init__(self):
        self.error = None

    async def get(self, server):
        if self.error:
            raise self.error
        return object(), None

    def drop(self, server_id):
        pass


class FakeEngine:
    def analyze(self, server_id, record):
        return record


def make_collector(monkeypatch, sessions, records, enabled=True):
    monkeypatch.setattr(runner, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "telegram_bot_token", "t" if enabled else "")
    monkeypatch.setattr(settings, "telegram_chat_id", "1" if enabled else "")

    async def collect(run, state):
        batch = list(records)
        records.clear()
        return CollectResult(batch, {})

    source = SimpleNamespace(name="fake", interval=0, collect=collect)
    monkeypatch.setattr(runner, "sources_for", lambda capabilities: [source])
    collector = runner.Collector(FakeEngine())
    collector.pool = FakePool()
    return collector


async def notifications(sessions):
    async with sessions() as s:
        return list(await s.scalars(select(Notification.text).order_by(Notification.id)))


def test_collector_queues_finding_alert_once(monkeypatch):
    async def test(sessions):
        records = [finding()]
        collector = make_collector(monkeypatch, sessions, records)
        async with sessions() as s:
            await add_server(s, capabilities=["x"])

        await collector.collect_server(1)
        records.append(finding())
        await collector.collect_server(1)

        queued = await notifications(sessions)
        assert len(queued) == 1
        assert "web-1" in queued[0]
        assert "제목-fp-1" in queued[0]

    with_db(test)


def test_collector_alerts_on_entering_and_leaving_unreachable(monkeypatch):
    async def test(sessions):
        collector = make_collector(monkeypatch, sessions, [])
        async with sessions() as s:
            await add_server(s, capabilities=["x"])

        collector.pool.error = OSError("connection refused")
        for _ in range(settings.unreachable_after_failures + 2):
            await collector.collect_server(1)
        queued = await notifications(sessions)
        assert len(queued) == 1
        assert "접속 불가" in queued[0]
        assert "connection refused" in queued[0]

        collector.pool.error = None
        await collector.collect_server(1)
        await collector.collect_server(1)
        queued = await notifications(sessions)
        assert len(queued) == 2
        assert "복구" in queued[1]

    with_db(test)


def test_finding_saved_before_a_later_source_fails_still_alerts(monkeypatch):
    async def test(sessions):
        collector = make_collector(monkeypatch, sessions, [finding()])

        async def broken(run, state):
            raise OSError("channel closed")

        async def collect(run, state):
            return CollectResult([finding()], {})

        sources = [
            SimpleNamespace(name="good", interval=0, collect=collect),
            SimpleNamespace(name="bad", interval=0, collect=broken),
        ]
        monkeypatch.setattr(runner, "sources_for", lambda capabilities: sources)
        async with sessions() as s:
            await add_server(s, capabilities=["x"])

        await collector.collect_server(1)

        queued = await notifications(sessions)
        assert len(queued) == 1
        assert "제목-fp-1" in queued[0]

    with_db(test)


def test_nothing_is_queued_while_telegram_is_not_configured(monkeypatch):
    async def test(sessions):
        collector = make_collector(monkeypatch, sessions, [finding()], enabled=False)
        async with sessions() as s:
            await add_server(s, capabilities=["x"])

        await collector.collect_server(1)

        assert await notifications(sessions) == []
        async with sessions() as s:
            assert await s.scalar(select(Event.fingerprint)) == "fp-1"

    with_db(test)
