import json
import shlex
from datetime import datetime, timezone

from app.collector.sources.base import CollectResult, RawRecord, Runner, Source

MAX_LINES = 2000


def parse_journal_json(output: str) -> tuple[list[RawRecord], str | None]:
    records: list[RawRecord] = []
    cursor = None
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        cursor = entry.get("__CURSOR", cursor)
        msg = entry.get("MESSAGE")
        if isinstance(msg, list):  # non-UTF8 messages come as byte arrays
            msg = bytes(msg).decode("utf-8", "replace")
        if not msg:
            continue
        ts = None
        if rt := entry.get("__REALTIME_TIMESTAMP"):
            ts = datetime.fromtimestamp(int(rt) / 1_000_000, tz=timezone.utc)
        prio = entry.get("PRIORITY")
        records.append(
            RawRecord(
                source="journal",
                message=str(msg),
                ts=ts,
                priority=int(prio) if prio is not None else None,
                unit=entry.get("_SYSTEMD_UNIT") or entry.get("SYSLOG_IDENTIFIER"),
            )
        )
    return records, cursor


class JournalSource(Source):
    name = "journal"
    interval = 60
    requires = ("journalctl",)

    async def collect(self, run: Runner, state: dict) -> CollectResult:
        base = f"journalctl -p err -o json --no-pager -n {MAX_LINES}"
        if cursor := state.get("cursor"):
            cmd = f"{base} --after-cursor={shlex.quote(cursor)}"
        else:
            # first run: only look back a short window instead of full history
            cmd = f"{base} --since=-10min"
        # monitor user should be in the systemd-journal group; fall back to sudo
        _, out = await run(f"{cmd} 2>/dev/null || sudo -n {cmd}")
        records, new_cursor = parse_journal_json(out)
        return CollectResult(records, {"cursor": new_cursor or state.get("cursor")})
