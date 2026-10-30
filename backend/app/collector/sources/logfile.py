import re

from app.collector.sources.base import CollectResult, RawRecord, Runner, Source

MAX_BYTES = 1024 * 1024  # per collection
# "Oct 31 10:12:01 host prog[123]: message" / RFC3339 timestamp variant
_SYSLOG = re.compile(r"^(?:\w{3}\s+\d+\s+[\d:]+|\d{4}-\d{2}-\d{2}T\S+)\s+\S+\s+([^:\[]+)(?:\[\d+\])?:\s*(.*)$")


def parse_syslog_lines(text: str, source: str) -> list[RawRecord]:
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _SYSLOG.match(line)
        if m:
            records.append(RawRecord(source=source, message=m[2], unit=m[1].strip()))
        else:
            records.append(RawRecord(source=source, message=line.strip()))
    return records


def split_header(output: str) -> tuple[int, int, str] | None:
    """Output format: first line '<inode> <size>', rest is file content."""
    header, _, body = output.partition("\n")
    try:
        inode, size = (int(x) for x in header.split())
    except ValueError:
        return None
    return inode, size, body


class LogFileSource(Source):
    """Incremental tail of a plain syslog file (hosts without journald).
    State: inode + byte offset; a changed inode or shrunk file means rotation."""

    interval = 60

    def __init__(self, path: str):
        self.path = path
        self.name = "file:" + path
        self.requires = (self.name,)

    async def collect(self, run: Runner, state: dict) -> CollectResult:
        _, out = await run(f"stat -c '%i %s' {self.path}")
        head = split_header(out + "\n")
        if head is None:
            return CollectResult([], state)
        inode, size, _ = head

        offset = state.get("offset")
        if offset is None:
            # first run: start from the end, don't replay history
            return CollectResult([], {"inode": inode, "offset": size})
        if state.get("inode") != inode or size < offset:
            offset = 0  # rotated

        if size == offset:
            return CollectResult([], {"inode": inode, "offset": offset})

        _, body = await run(f"tail -c +{offset + 1} {self.path} | head -c {MAX_BYTES}")
        # only consume complete lines; a partial last line is read next time
        consumed = body.rfind("\n") + 1
        text = body[:consumed]
        return CollectResult(
            parse_syslog_lines(text, self.name),
            {"inode": inode, "offset": offset + len(text.encode())},
        )


LOG_FILES = ("/var/log/syslog", "/var/log/messages")
