from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.analyzer.rule_engine import Finding
from app.collector.sources import sources_for
from app.core.config import settings
from app.models import Event, EventOccurrence, Server, SourceStatus, utcnow
from app.notifier.alerts import should_alert


async def upsert_findings(
    session: AsyncSession, server_id: int, findings: list[Finding]
) -> list[Finding]:
    """Insert new findings; repeats bump count/last_seen and re-open acknowledged events.
    Returns the findings that should alert."""
    if not findings:
        return []
    # fingerprint -> acknowledged, as it was before this batch
    known = dict(
        (
            await session.execute(
                select(Event.fingerprint, Event.acknowledged).where(
                    Event.server_id == server_id,
                    Event.fingerprint.in_({f.fingerprint for f in findings}),
                )
            )
        ).all()
    )
    alerts = []
    for f in findings:
        if should_alert(f.severity, known.get(f.fingerprint), settings.telegram_min_severity):
            alerts.append(f)
        known[f.fingerprint] = False  # open from here on, whether it alerted or not
        seen = f.record.ts or utcnow()
        stmt = insert(Event).values(
            server_id=server_id,
            source=f.source,
            rule_id=f.rule_id,
            category=f.category,
            severity=f.severity,
            title=f.title,
            message=f.message,
            fingerprint=f.fingerprint,
            count=1,
            first_seen=seen,
            last_seen=seen,
            acknowledged=False,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Event.server_id, Event.fingerprint],
            set_={
                "count": Event.count + 1,
                "last_seen": func.greatest(Event.last_seen, stmt.excluded.last_seen),
                "message": stmt.excluded.message,
                "acknowledged": False,
            },
        ).returning(Event.id)
        event_id = (await session.execute(stmt)).scalar_one()
        await session.execute(
            insert(EventOccurrence).values(event_id=event_id, message=f.message, seen_at=seen)
        )
    return alerts


def decide_status(consecutive_failures: int, severity_counts: dict, source_failed: bool) -> str:
    if consecutive_failures >= settings.unreachable_after_failures:
        return "unreachable"
    if severity_counts.get("critical"):
        return "critical"
    if severity_counts.get("warning"):
        return "warning"
    # "ok" must mean logs were actually read and nothing was wrong
    return "degraded" if source_failed else "ok"


def select_active(
    capabilities: list[str], rows: Iterable[SourceStatus]
) -> list[tuple[str, SourceStatus | None]]:
    """Sources currently enabled for a server, paired with their latest run (None
    if not run yet). Rows of sources no longer enabled are stale and left out."""
    by_source = {r.source: r for r in rows}
    return [(s.name, by_source.get(s.name)) for s in sources_for(capabilities)]


def summarize_failures(rows: Iterable[SourceStatus]) -> str | None:
    return "; ".join(f"{r.source}: {r.error}" for r in rows if not r.ok) or None


def apply_source_run(
    status: SourceStatus,
    *,
    ok: bool,
    exit_status: int | None,
    error: str | None,
    command: str | None,
    records: int,
    findings: int,
    duration_ms: int,
    now: datetime,
) -> None:
    status.ok = ok
    status.exit_status = exit_status
    status.error = error
    status.command = command
    status.records = records
    status.findings = findings
    status.duration_ms = duration_ms
    status.last_run_at = now
    if ok:
        status.last_success_at = now


async def load_source_rows(session: AsyncSession, server_id: int) -> list[SourceStatus]:
    return list(await session.scalars(select(SourceStatus).where(SourceStatus.server_id == server_id)))


async def compute_status(session: AsyncSession, server: Server, source_failed: bool) -> str:
    since = utcnow() - timedelta(hours=settings.status_window_hours)
    rows = await session.execute(
        select(Event.severity, func.count())
        .where(
            Event.server_id == server.id,
            Event.acknowledged.is_(False),
            Event.last_seen >= since,
        )
        .group_by(Event.severity)
    )
    return decide_status(server.consecutive_failures, dict(rows.all()), source_failed)
