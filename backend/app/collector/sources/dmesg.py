import re
from datetime import datetime

from app.collector.sources.base import CollectResult, RawRecord, Runner, Source

# "2026-10-31T10:12:01,123456+09:00 kernel message"
_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}),(\d+)([+-]\d{2}:\d{2})\s+(.*)$")


def parse_dmesg_iso(output: str, after: str | None) -> tuple[list[RawRecord], str | None]:
    records: list[RawRecord] = []
    last = after
    for line in output.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        ts = datetime.fromisoformat(f"{m[1]}.{m[2][:6]}{m[3]}")
        key = ts.isoformat()
        if after and key <= after:
            continue
        records.append(RawRecord(source="dmesg", message=m[4], ts=ts, priority=3))
        if last is None or key > last:
            last = key
    return records, last


class DmesgSource(Source):
    """Kernel ring buffer. Mostly useful on hosts without journald;
    on journald hosts kernel errors also arrive via the journal source."""

    name = "dmesg"
    interval = 60
    requires = ("dmesg",)

    def enabled_for(self, capabilities: list[str]) -> bool:
        return "dmesg" in capabilities and "journalctl" not in capabilities

    async def collect(self, run: Runner, state: dict) -> CollectResult:
        cmd = "dmesg --level=err,crit,alert,emerg --time-format iso"
        _, out = await run(f"{cmd} || sudo -n {cmd}")
        records, last = parse_dmesg_iso(out, state.get("last_ts"))
        if state.get("last_ts") is None:
            # first run: establish a baseline without reporting the whole buffer
            records = []
        return CollectResult(records, {"last_ts": last})
