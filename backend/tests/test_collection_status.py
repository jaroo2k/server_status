import asyncio
import json
import os
import stat
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.analyzer.store import (
    apply_source_run,
    decide_status,
    select_active,
    summarize_failures,
)
from app.collector.sources.base import CommandResult
from app.collector.sources.dmesg import DmesgSource
from app.collector.sources.journal import JournalSource
from app.collector.sources.logfile import LogFileSource
from app.collector.ssh_pool import run_command
from app.collector.tracking import MAX_ERROR_CHARS, TrackingRunner
from app.models import SourceStatus

T1 = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc)


def stub(bin_dir, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def shell(bin_dir):
    """Runs commands through a real shell, with stub tools first on PATH."""

    async def execute(command: str) -> CommandResult:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"},
        )
        out, err = await proc.communicate()
        return CommandResult(proc.returncode, out.decode(), err.decode())

    return execute


NO_PERMISSION = 'echo "No journal files were opened due to insufficient permissions" >&2; exit 1'
NO_SUDO = 'echo "sudo: a password is required" >&2; exit 1'


def test_journal_failure_exposes_reason_from_both_attempts(tmp_path):
    stub(tmp_path, "journalctl", NO_PERMISSION)
    stub(tmp_path, "sudo", NO_SUDO)
    tracker = TrackingRunner(shell(tmp_path))

    result = asyncio.run(JournalSource().collect(tracker, {}))

    assert result.records == []
    assert tracker.failure.exit_status == 1
    assert "insufficient permissions" in tracker.failure.error
    assert "a password is required" in tracker.failure.error


def test_journal_sudo_fallback_success_is_not_a_failure(tmp_path):
    entry = json.dumps({"__CURSOR": "c1", "PRIORITY": "3", "MESSAGE": "disk error"})
    stub(tmp_path, "journalctl", NO_PERMISSION)
    stub(tmp_path, "sudo", f"echo '{entry}'")
    tracker = TrackingRunner(shell(tmp_path))

    result = asyncio.run(JournalSource().collect(tracker, {}))

    assert tracker.failure is None
    assert [r.message for r in result.records] == ["disk error"]
    assert result.new_state == {"cursor": "c1"}


def test_dmesg_failure_exposes_reason(tmp_path):
    stub(tmp_path, "dmesg", 'echo "dmesg: read kernel buffer failed: Operation not permitted" >&2; exit 1')
    stub(tmp_path, "sudo", NO_SUDO)
    tracker = TrackingRunner(shell(tmp_path))

    asyncio.run(DmesgSource().collect(tracker, {}))

    assert "Operation not permitted" in tracker.failure.error


def test_tracker_reports_first_failing_command_not_a_later_one():
    answers = {
        "first": CommandResult(2, "", "first broke"),
        "second": CommandResult(1, "", "second broke"),
    }

    async def execute(command):
        return answers[command]

    tracker = TrackingRunner(execute)

    async def scenario():
        assert await tracker("first") == (2, "")
        assert await tracker("second") == (1, "")

    asyncio.run(scenario())
    assert tracker.failure.command == "first"
    assert tracker.failure.exit_status == 2
    assert "first broke" in tracker.failure.error
    assert tracker.last_command == "second"


def test_tracker_success_has_no_failure_and_passes_stdout_through():
    async def execute(command):
        return CommandResult(0, "output", "harmless hint on stderr")

    tracker = TrackingRunner(execute)

    assert asyncio.run(tracker("cmd")) == (0, "output")
    assert tracker.failure is None
    assert tracker.last_command == "cmd"


def test_logfile_stat_failure_is_reported():
    async def execute(command):
        return CommandResult(1, "", "stat: cannot statx: Permission denied")

    tracker = TrackingRunner(execute)
    state = {"inode": 1, "offset": 5}

    result = asyncio.run(LogFileSource("/var/log/syslog").collect(tracker, state))

    assert "Permission denied" in tracker.failure.error
    assert result.new_state == state


def test_failure_without_stderr_still_names_the_exit_status():
    async def execute(command):
        return CommandResult(3, "", "")

    tracker = TrackingRunner(execute)
    asyncio.run(tracker("cmd"))

    assert "3" in tracker.failure.error


def test_failure_error_is_safe_to_store():
    # Postgres rejects NUL in text columns, and stderr can be arbitrarily long.
    async def execute(command):
        return CommandResult(1, "", "a\x00b" + "x" * (MAX_ERROR_CHARS * 2))

    tracker = TrackingRunner(execute)
    asyncio.run(tracker("cmd"))

    assert "\x00" not in tracker.failure.error
    assert len(tracker.failure.error) <= MAX_ERROR_CHARS


def conn_returning(**fields):
    async def run(command, check):
        assert check is False
        return SimpleNamespace(**fields)

    return SimpleNamespace(run=run)


def test_run_command_returns_stderr():
    conn = conn_returning(exit_status=1, stdout="out", stderr="err")

    result = asyncio.run(run_command(conn, "cmd"))

    assert result == CommandResult(1, "out", "err")


def test_run_command_treats_signal_killed_command_as_failure():
    # asyncssh reports exit_status None when the remote command died from a signal.
    conn = conn_returning(exit_status=None, stdout="", stderr="")

    assert asyncio.run(run_command(conn, "cmd")).exit_status != 0


@pytest.mark.parametrize(
    "failures, counts, source_failed, want",
    [
        (0, {}, False, "ok"),
        (0, {}, True, "degraded"),
        (0, {"warning": 1}, True, "warning"),
        (0, {"critical": 1, "warning": 1}, True, "critical"),
        (2, {}, False, "ok"),
        (3, {"critical": 1}, True, "unreachable"),
    ],
)
def test_decide_status(failures, counts, source_failed, want):
    assert decide_status(failures, counts, source_failed) == want


def row(source, ok=True, error=None):
    return SourceStatus(server_id=1, source=source, ok=ok, error=error)


def test_select_active_lists_pending_sources_and_hides_stale_ones():
    journal, stale = row("journal"), row("dmesg")

    active = select_active(["journalctl", "systemctl", "dmesg"], [journal, stale])

    assert active == [("journal", journal), ("systemd", None)]


def test_summarize_failures_names_only_failed_sources():
    rows = [row("journal", ok=False, error="exit 1: denied"), row("systemd")]

    assert summarize_failures(rows) == "journal: exit 1: denied"


def test_summarize_failures_is_empty_when_all_sources_work():
    assert summarize_failures([row("journal"), row("systemd")]) is None


def test_apply_source_run_success_then_failure_keeps_last_success_time():
    status = row("journal")

    apply_source_run(
        status, ok=True, exit_status=0, error=None, command="c1",
        records=4, findings=1, duration_ms=12, now=T1,
    )
    assert (status.ok, status.last_run_at, status.last_success_at) == (True, T1, T1)
    assert (status.records, status.findings) == (4, 1)

    apply_source_run(
        status, ok=False, exit_status=1, error="exit 1: denied", command="c2",
        records=0, findings=0, duration_ms=9, now=T2,
    )
    assert (status.ok, status.last_run_at, status.last_success_at) == (False, T2, T1)
    assert (status.error, status.command) == ("exit 1: denied", "c2")


def test_apply_source_run_recovery_clears_the_error():
    status = row("journal", ok=False, error="exit 1: denied")

    apply_source_run(
        status, ok=True, exit_status=0, error=None, command="c",
        records=0, findings=0, duration_ms=5, now=T2,
    )

    assert status.ok is True and status.error is None
