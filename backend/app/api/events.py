from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.models import Event, EventOccurrence, Server
from app.schemas import EventOut, OccurrenceOut, OccurrencePage, Summary

router = APIRouter(prefix="/api", tags=["events"])


@router.get("/events", response_model=list[EventOut])
async def list_events(
    server_id: int | None = None,
    severity: str | None = None,
    category: str | None = None,
    acknowledged: bool | None = None,
    since: datetime | None = None,
    q: str | None = Query(None, description="substring search in title/message"),
    limit: int = Query(100, le=1000),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Event, Server.name).join(Server, Server.id == Event.server_id)
    if server_id is not None:
        stmt = stmt.where(Event.server_id == server_id)
    if severity:
        stmt = stmt.where(Event.severity == severity)
    if category:
        stmt = stmt.where(Event.category == category)
    if acknowledged is not None:
        stmt = stmt.where(Event.acknowledged.is_(acknowledged))
    if since:
        stmt = stmt.where(Event.last_seen >= since)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Event.title.ilike(like) | Event.message.ilike(like))
    stmt = stmt.order_by(Event.last_seen.desc()).limit(limit).offset(offset)
    rows = (await session.execute(stmt)).all()
    return [
        EventOut.model_validate(ev, from_attributes=True).model_copy(update={"server_name": name})
        for ev, name in rows
    ]


@router.get("/events/{event_id}", response_model=EventOut)
async def get_event(event_id: int, session: AsyncSession = Depends(get_session)):
    row = (
        await session.execute(
            select(Event, Server.name)
            .join(Server, Server.id == Event.server_id)
            .where(Event.id == event_id)
        )
    ).first()
    if row is None:
        raise HTTPException(404, "event not found")
    ev, name = row
    return EventOut.model_validate(ev, from_attributes=True).model_copy(update={"server_name": name})


@router.get("/occurrences", response_model=OccurrencePage)
async def list_occurrences(
    server_id: int | None = None,
    event_id: int | None = None,
    severity: str | None = None,
    from_: datetime | None = Query(None, alias="from", description="inclusive"),
    to: datetime | None = Query(None, description="exclusive"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    """Raw log lines across events, newest first."""
    filters = []
    if server_id is not None:
        filters.append(Event.server_id == server_id)
    if event_id is not None:
        filters.append(EventOccurrence.event_id == event_id)
    if severity:
        filters.append(Event.severity == severity)
    if from_ is not None:
        filters.append(EventOccurrence.seen_at >= from_)
    if to is not None:
        filters.append(EventOccurrence.seen_at < to)

    total = await session.scalar(
        select(func.count())
        .select_from(EventOccurrence)
        .join(Event, Event.id == EventOccurrence.event_id)
        .where(*filters)
    )
    rows = await session.execute(
        select(EventOccurrence, Event, Server.name)
        .join(Event, Event.id == EventOccurrence.event_id)
        .join(Server, Server.id == Event.server_id)
        .where(*filters)
        .order_by(EventOccurrence.seen_at.desc(), EventOccurrence.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return OccurrencePage(
        total=total,
        items=[
            OccurrenceOut(
                id=line.id,
                event_id=ev.id,
                server_id=ev.server_id,
                server_name=name,
                source=ev.source,
                rule_id=ev.rule_id,
                severity=ev.severity,
                title=ev.title,
                message=line.message,
                seen_at=line.seen_at,
            )
            for line, ev, name in rows
        ],
    )


@router.post("/events/{event_id}/ack", response_model=EventOut)
async def ack_event(event_id: int, session: AsyncSession = Depends(get_session)):
    ev = await session.get(Event, event_id)
    if ev is None:
        raise HTTPException(404, "event not found")
    ev.acknowledged = True
    await session.commit()
    return EventOut.model_validate(ev, from_attributes=True)


@router.get("/summary", response_model=Summary)
async def summary(session: AsyncSession = Depends(get_session)):
    by_status = dict(
        (await session.execute(select(Server.status, func.count()).group_by(Server.status))).all()
    )
    open_events = select(Event).where(Event.acknowledged.is_(False)).subquery()
    by_sev = dict(
        (
            await session.execute(
                select(open_events.c.severity, func.count()).group_by(open_events.c.severity)
            )
        ).all()
    )
    by_cat = dict(
        (
            await session.execute(
                select(open_events.c.category, func.count()).group_by(open_events.c.category)
            )
        ).all()
    )
    return Summary(
        servers_total=sum(by_status.values()),
        servers_by_status=by_status,
        open_events_by_severity=by_sev,
        open_events_by_category=by_cat,
    )
