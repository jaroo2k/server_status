"""Collector worker: `python -m app.collector.runner`.

Each tick, picks servers owned by this worker shard and runs every source whose
interval has elapsed. Concurrency is bounded by a semaphore so 50+ servers can
be polled without opening unbounded SSH sessions.
"""

import asyncio
import logging
import time

from sqlalchemy import select

from app.analyzer.rule_engine import RuleEngine, load_rules
from app.analyzer.store import (
    apply_source_run,
    compute_status,
    load_source_rows,
    select_active,
    summarize_failures,
    upsert_findings,
)
from app.collector.capabilities import DETECT_SCRIPT, parse_capabilities
from app.collector.sources import sources_for
from app.collector.sources.base import CommandResult
from app.collector.ssh_pool import HostKeyChanged, SSHPool, run_command
from app.collector.tracking import TrackingRunner
from app.core.config import settings
from app.core.db import SessionLocal, init_db
from app.models import CollectorState, Server, SourceStatus, utcnow

log = logging.getLogger("collector")


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class Collector:
    def __init__(self, engine: RuleEngine):
        self.engine = engine
        self.pool = SSHPool()
        self.sem = asyncio.Semaphore(settings.max_concurrency)
        self._last_run: dict[tuple[int, str], float] = {}
        self._busy: set[int] = set()
        self._tasks: set[asyncio.Task] = set()

    async def tick(self) -> None:
        async with SessionLocal() as session:
            servers = (
                await session.scalars(
                    select(Server).where(
                        Server.enabled.is_(True),
                        Server.id % settings.worker_count == settings.worker_index,
                    )
                )
            ).all()
        # Don't wait for slow/hung servers: they keep their slot in _busy and are
        # skipped by later ticks until they finish.
        for s in servers:
            if s.id not in self._busy:
                self._busy.add(s.id)
                task = asyncio.create_task(self._guarded(s.id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _guarded(self, server_id: int) -> None:
        try:
            async with self.sem:
                await self.collect_server(server_id)
        except Exception:
            log.exception("unexpected collector error for server %s", server_id)
        finally:
            self._busy.discard(server_id)

    async def collect_server(self, server_id: int) -> None:
        async with SessionLocal() as session:
            server = await session.get(Server, server_id)
            if server is None:
                return
            now = time.monotonic()
            try:
                conn, host_key = await self.pool.get(server)
                if host_key and not server.host_key:
                    server.host_key = host_key

                async def run(cmd: str) -> CommandResult:
                    return await run_command(conn, cmd)

                if not server.capabilities:
                    server.capabilities = parse_capabilities((await run(DETECT_SCRIPT)).stdout)

                due = [
                    s
                    for s in sources_for(server.capabilities)
                    if now - self._last_run.get((server.id, s.name), 0) >= s.interval
                ]
                for src in due:
                    st = await session.get(CollectorState, (server.id, src.name))
                    if st is None:
                        st = CollectorState(server_id=server.id, source=src.name, state={})
                        session.add(st)
                    status = await session.get(SourceStatus, (server.id, src.name))
                    if status is None:
                        status = SourceStatus(server_id=server.id, source=src.name)
                    tracker = TrackingRunner(run)
                    started = time.monotonic()
                    try:
                        result = await src.collect(tracker, dict(st.state or {}))
                    except Exception as e:
                        apply_source_run(
                            status,
                            ok=False,
                            exit_status=None,
                            error=f"{type(e).__name__}: {e}",
                            command=tracker.last_command,
                            records=0,
                            findings=0,
                            duration_ms=_elapsed_ms(started),
                            now=utcnow(),
                        )
                        session.add(status)
                        raise
                    findings = [
                        f for r in result.records if (f := self.engine.analyze(server.id, r))
                    ]
                    await upsert_findings(session, server.id, findings)
                    st.state = result.new_state
                    failure = tracker.failure
                    apply_source_run(
                        status,
                        ok=failure is None,
                        exit_status=failure.exit_status if failure else 0,
                        error=failure.error if failure else None,
                        command=failure.command if failure else tracker.last_command,
                        records=len(result.records),
                        findings=len(findings),
                        duration_ms=_elapsed_ms(started),
                        now=utcnow(),
                    )
                    session.add(status)
                    self._last_run[(server.id, src.name)] = now
                    if failure:
                        log.warning("%s/%s: %s", server.name, src.name, failure.error)
                    if findings:
                        log.info("%s/%s: %d findings", server.name, src.name, len(findings))

                server.consecutive_failures = 0
                server.last_error = None
                server.last_collected_at = utcnow()
            except HostKeyChanged as e:
                # never auto-trust a changed key; requires admin action
                self.pool.drop(server.id)
                server.consecutive_failures += 1
                server.last_error = str(e)
                log.error("%s", e)
            except Exception as e:  # noqa: BLE001  (network/auth/timeout: mark and move on)
                self.pool.drop(server.id)
                server.consecutive_failures += 1
                server.last_error = f"{type(e).__name__}: {e}"
                log.warning("%s: collection failed: %s", server.name, server.last_error)

            active = select_active(
                server.capabilities, await load_source_rows(session, server.id)
            )
            source_error = summarize_failures(row for _, row in active if row)
            if server.last_error is None:
                server.last_error = source_error
            server.status = await compute_status(
                session, server, source_failed=source_error is not None
            )
            await session.commit()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_db()
    collector = Collector(RuleEngine(load_rules(settings.rules_dir)))
    log.info(
        "collector started (shard %d/%d, concurrency %d)",
        settings.worker_index,
        settings.worker_count,
        settings.max_concurrency,
    )
    try:
        while True:
            started = time.monotonic()
            await collector.tick()
            await asyncio.sleep(max(1.0, settings.collect_tick_seconds - (time.monotonic() - started)))
    finally:
        collector.pool.close_all()


if __name__ == "__main__":
    asyncio.run(main())
