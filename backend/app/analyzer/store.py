from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.analyzer.rule_engine import Finding
from app.core.config import settings
from app.models import Event, Server, utcnow


async def upsert_findings(session: AsyncSession, server_id: int, findings: list[Finding]) -> None:
    """Insert new findings; repeats bump count/last_seen and re-open acknowledged events."""
    for f in findings:
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
        )
        await session.execute(stmt)


async def compute_status(session: AsyncSession, server: Server) -> str:
    if server.consecutive_failures >= settings.unreachable_after_failures:
        return "unreachable"
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
    counts = dict(rows.all())
    if counts.get("critical"):
        return "critical"
    if counts.get("warning"):
        return "warning"
    return "ok"
