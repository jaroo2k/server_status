"""Needs real Postgres, see test_notifier_db.py."""

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text

from app.analyzer.store import upsert_findings
from app.api.events import get_event, list_events, list_occurrences
from app.core.db import create_missing_indexes
from app.models import Event, EventOccurrence, Server
from tests.test_notifier_db import add_server, with_db

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"
)

T = datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc)


def finding(
    message, ts=T, fingerprint="fp-1", rule_id="disk.io_error", severity="warning", source="journal"
):
    return SimpleNamespace(
        rule_id=rule_id,
        title=f"제목-{fingerprint}",
        category="hardware",
        severity=severity,
        source=source,
        message=message,
        fingerprint=fingerprint,
        record=SimpleNamespace(ts=ts),
    )


async def event_id(session, fingerprint="fp-1"):
    return await session.scalar(select(Event.id).where(Event.fingerprint == fingerprint))


async def page(
    session, server_id=None, event_id=None, severity=None, from_=None, to=None, limit=100, offset=0
):
    return await list_occurrences(
        server_id=server_id,
        event_id=event_id,
        severity=severity,
        from_=from_,
        to=to,
        limit=limit,
        offset=offset,
        session=session,
    )


async def lines(session, **filters):
    return [o.message for o in (await page(session, **filters)).items]


async def events(session):
    return await list_events(
        server_id=None,
        severity=None,
        category=None,
        acknowledged=None,
        since=None,
        q=None,
        limit=100,
        offset=0,
        session=session,
    )


# ---------- what is kept ----------
def test_each_repeat_keeps_its_own_raw_line_newest_first():
    async def test(sessions):
        async with sessions() as s:
            await add_server(s)
            await upsert_findings(s, 1, [finding("sector 1", T + timedelta(minutes=1))])
            await upsert_findings(s, 1, [finding("sector 2", T + timedelta(minutes=2))])
            # a line stamped earlier than what was already collected
            await upsert_findings(s, 1, [finding("sector 0", T)])

            assert await lines(s) == ["sector 2", "sector 1", "sector 0"]

    with_db(test)


def test_every_line_is_kept_however_often_an_event_repeats():
    async def test(sessions):
        async with sessions() as s:
            await add_server(s)
            batch = [finding(f"line {i}", T + timedelta(seconds=i)) for i in range(53)]
            await upsert_findings(s, 1, batch)
            await upsert_findings(s, 1, [finding("line 53", T + timedelta(seconds=53))])

            result = await page(s, limit=1000)

            assert result.total == 54
            assert result.items[-1].message == "line 0"

    with_db(test)


def test_still_failed_unit_keeps_a_line_only_when_new_or_reopened():
    async def test(sessions):
        async with sessions() as s:
            await add_server(s)

            async def report(minute, repeats=1):
                unit = finding(f"run {minute}", T + timedelta(minutes=minute), source="systemd")
                await upsert_findings(s, 1, [unit] * repeats)

            await report(0, repeats=2)  # new, then still failed in the same batch
            await report(1)  # still failed
            ev = await s.scalar(select(Event))
            ev.acknowledged = True
            await s.flush()
            await report(2)  # reopened
            await report(3)  # still failed

            assert await lines(s) == ["run 2", "run 0"]
            await s.refresh(ev)
            assert ev.count == 5

    with_db(test)


def test_deleting_a_server_removes_its_history():
    async def test(sessions):
        async with sessions() as s:
            await add_server(s)
            await upsert_findings(s, 1, [finding("sector 1")])
            await s.execute(delete(Server))

            assert await s.scalar(select(func.count()).select_from(EventOccurrence)) == 0

    with_db(test)


# ---------- what the list shows ----------
def test_line_says_which_event_and_server_it_belongs_to():
    async def test(sessions):
        async with sessions() as s:
            await add_server(s)
            await upsert_findings(s, 1, [finding("sector 1")])

            line = (await page(s)).items[0]

            assert line.event_id == await event_id(s)
            assert line.seen_at == T
            assert (line.server_id, line.server_name) == (1, "web-1")
            assert (line.title, line.severity, line.rule_id, line.source) == (
                "제목-fp-1",
                "warning",
                "disk.io_error",
                "journal",
            )

    with_db(test)


def test_date_range_includes_from_and_excludes_to():
    async def test(sessions):
        async with sessions() as s:
            await add_server(s)
            day = timedelta(days=1)
            await upsert_findings(
                s,
                1,
                [
                    finding("just before", T - timedelta(microseconds=1)),
                    finding("at from", T),
                    finding("just inside", T + day - timedelta(microseconds=1)),
                    finding("at to", T + day),
                ],
            )

            assert await lines(s, from_=T, to=T + day) == ["just inside", "at from"]
            assert await lines(s, from_=T + day) == ["at to"]
            assert await lines(s, to=T) == ["just before"]

    with_db(test)


def test_filters_by_event_and_by_server():
    async def test(sessions):
        async with sessions() as s:
            await add_server(s, 1)
            await add_server(s, 2)
            await upsert_findings(s, 1, [finding("one", fingerprint="fp-1")])
            await upsert_findings(s, 1, [finding("other event", fingerprint="fp-2")])
            await upsert_findings(s, 2, [finding("two", fingerprint="fp-1")])

            one = await s.scalar(
                select(Event.id).where(Event.server_id == 1, Event.fingerprint == "fp-1")
            )

            assert await lines(s, event_id=one) == ["one"]
            assert sorted(await lines(s, server_id=1)) == ["one", "other event"]
            assert await lines(s, server_id=2) == ["two"]

    with_db(test)


def test_filters_by_severity_and_total_follows():
    async def test(sessions):
        async with sessions() as s:
            await add_server(s)
            await upsert_findings(
                s,
                1,
                [
                    finding("noise", fingerprint="fp-w", severity="warning"),
                    finding("noise again", fingerprint="fp-w", severity="warning"),
                    finding("serious", fingerprint="fp-c", severity="critical"),
                ],
            )

            critical = await page(s, severity="critical")

            assert [o.message for o in critical.items] == ["serious"]
            assert critical.total == 1
            assert (await page(s, severity="warning")).total == 2

    with_db(test)


def test_pages_do_not_overlap_and_total_counts_all_matches():
    async def test(sessions):
        async with sessions() as s:
            await add_server(s)
            await upsert_findings(
                s, 1, [finding(f"line {i}", T + timedelta(seconds=i)) for i in range(5)]
            )

            first = await page(s, limit=2)
            second = await page(s, limit=2, offset=2)
            filtered = await page(s, limit=1, from_=T + timedelta(seconds=3))

            assert [o.message for o in first.items] == ["line 4", "line 3"]
            assert [o.message for o in second.items] == ["line 2", "line 1"]
            assert first.total == 5
            assert filtered.total == 2

    with_db(test)


def test_total_stops_counting_at_the_cap_but_next_page_stays_reachable(monkeypatch):
    monkeypatch.setattr("app.api.events.TOTAL_CAP", 3)

    async def test(sessions):
        async with sessions() as s:
            await add_server(s)
            await upsert_findings(
                s, 1, [finding(f"line {i}", T + timedelta(seconds=i)) for i in range(6)]
            )

            first = await page(s, limit=2)
            assert (first.total, first.total_capped) == (3, True)

            # deeper pages count further, so "shown < total" still finds the next page
            deeper = await page(s, limit=2, offset=2)
            assert (deeper.total, deeper.total_capped) == (5, True)
            last = await page(s, limit=2, offset=4)
            assert (last.total, last.total_capped) == (6, False)

            # exactly at the cap is an exact count
            monkeypatch.setattr("app.api.events.TOTAL_CAP", 6)
            assert (await page(s)).total_capped is False

    with_db(test)


# ---------- single event, opened from a history line ----------
def test_single_event_is_found_even_when_acknowledged():
    async def test(sessions):
        async with sessions() as s:
            await add_server(s)
            await upsert_findings(s, 1, [finding("sector 1")])
            ev = await s.scalar(select(Event))
            ev.acknowledged = True
            await s.flush()

            found = await get_event(ev.id, s)

            assert found.id == ev.id
            assert found.server_name == "web-1"
            assert "디스크" in found.description

    with_db(test)


def test_unknown_event_is_404():
    async def test(sessions):
        async with sessions() as s:
            with pytest.raises(HTTPException) as e:
                await get_event(999, s)

            assert e.value.status_code == 404

    with_db(test)


def test_event_list_explains_each_event_by_its_rule():
    async def test(sessions):
        async with sessions() as s:
            await add_server(s)
            await upsert_findings(
                s,
                1,
                [
                    finding("a", fingerprint="fp-disk", rule_id="disk.io_error"),
                    finding("b", fingerprint="fp-oom", rule_id="mem.oom"),
                ],
            )

            described = {e.rule_id: e.description for e in await events(s)}

            assert "디스크" in described["disk.io_error"]
            assert "메모리" in described["mem.oom"]

    with_db(test)


# ---------- schema ----------
def test_index_missing_from_an_existing_table_is_created():
    async def test(sessions):
        async with sessions() as s:
            await s.execute(text("DROP INDEX ix_event_occurrences_seen_at"))
            conn = await s.connection()

            await conn.run_sync(create_missing_indexes)
            await conn.run_sync(create_missing_indexes)  # safe on every startup

            names = await s.scalars(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'event_occurrences'")
            )
            assert "ix_event_occurrences_seen_at" in set(names)

    with_db(test)
