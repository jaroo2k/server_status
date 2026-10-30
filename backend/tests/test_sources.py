import asyncio
import json

from app.collector.capabilities import parse_capabilities
from app.collector.sources import sources_for
from app.collector.sources.dmesg import parse_dmesg_iso
from app.collector.sources.journal import parse_journal_json
from app.collector.sources.logfile import LogFileSource, parse_syslog_lines
from app.collector.sources.systemd import parse_failed_units


def test_journal_json_parses_messages_and_cursor():
    lines = [
        {"__CURSOR": "s=a;i=1", "__REALTIME_TIMESTAMP": "1761900000000000", "PRIORITY": "3",
         "MESSAGE": "Buffer I/O error on dev sda1", "SYSLOG_IDENTIFIER": "kernel"},
        {"__CURSOR": "s=a;i=2", "PRIORITY": "2", "MESSAGE": list(b"bytes msg"),
         "_SYSTEMD_UNIT": "nginx.service"},
    ]
    out = "\n".join(json.dumps(x) for x in lines) + "\n-- garbage --\n"
    records, cursor = parse_journal_json(out)
    assert cursor == "s=a;i=2"
    assert [r.message for r in records] == ["Buffer I/O error on dev sda1", "bytes msg"]
    assert records[0].priority == 3 and records[0].unit == "kernel"
    assert records[0].ts.year == 2025
    assert records[1].unit == "nginx.service"


def test_dmesg_filters_already_seen():
    out = (
        "2026-10-31T10:00:00,100000+09:00 old error\n"
        "2026-10-31T10:05:00,200000+09:00 EXT4-fs error (device sda1)\n"
    )
    records, last = parse_dmesg_iso(out, after="2026-10-31T10:00:00.100000+09:00")
    assert [r.message for r in records] == ["EXT4-fs error (device sda1)"]
    assert last == "2026-10-31T10:05:00.200000+09:00"


def test_failed_units():
    out = "nginx.service loaded failed failed A high performance web server\nfoo.mount loaded active mounted Foo\n"
    records = parse_failed_units(out)
    assert len(records) == 1
    assert records[0].unit == "nginx.service"


def test_syslog_lines():
    recs = parse_syslog_lines(
        "Oct 31 10:12:01 web1 kernel: [123.4] Out of memory: Killed process 42\nraw line\n", "file:x"
    )
    assert recs[0].unit == "kernel"
    assert recs[0].message.startswith("[123.4] Out of memory")
    assert recs[1].message == "raw line"


def test_capability_selection():
    caps = parse_capabilities("journalctl\ndmesg\nsystemctl\nfile:/var/log/syslog\n")
    names = {s.name for s in sources_for(caps)}
    # journald present: no dmesg / file duplicates
    assert names == {"journal", "systemd"}
    names = {s.name for s in sources_for(["dmesg", "file:/var/log/messages"])}
    assert names == {"dmesg", "file:/var/log/messages"}


class FakeHost:
    def __init__(self):
        self.content = b""
        self.inode = 1

    async def run(self, cmd: str):
        if cmd.startswith("stat"):
            return 0, f"{self.inode} {len(self.content)}\n"
        if cmd.startswith("tail"):
            offset = int(cmd.split("+")[1].split()[0]) - 1
            return 0, self.content[offset:].decode()
        raise AssertionError(cmd)


def test_logfile_incremental_and_rotation():
    host, src = FakeHost(), LogFileSource("/var/log/syslog")
    host.content = b"Oct 31 10:00:00 h app: history\n"

    async def scenario():
        r = await src.collect(host.run, {})
        assert r.records == [] and r.new_state["offset"] == len(host.content)  # baseline
        host.content += "Oct 31 10:01:00 h app: 새 에러 one\nOct 31 10:01:01 h app: partial".encode()
        r = await src.collect(host.run, r.new_state)
        assert [x.message for x in r.records] == ["새 에러 one"]
        host.content += b" line\n"
        r = await src.collect(host.run, r.new_state)
        assert [x.message for x in r.records] == ["partial line"]
        host.content, host.inode = b"Oct 31 11:00:00 h app: after rotate\n", 2
        r = await src.collect(host.run, r.new_state)
        assert [x.message for x in r.records] == ["after rotate"]

    asyncio.run(scenario())
