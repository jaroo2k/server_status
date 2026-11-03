import pytest

from app.analyzer.fingerprint import fingerprint, normalize
from app.analyzer.rule_engine import RuleEngine, describe_rule, load_rules
from app.collector.sources.base import RawRecord
from app.core.config import settings


@pytest.fixture(scope="module")
def engine():
    return RuleEngine(load_rules(settings.rules_dir))


@pytest.mark.parametrize(
    "message,rule_id,category,severity",
    [
        ("blk_update_request: I/O error, dev sda, sector 12345", "disk.io_error", "hardware", "critical"),
        ("Buffer I/O error on dev sdb1, logical block 0", "disk.io_error", "hardware", "critical"),
        ("ata3.00: failed command: READ FPDMA QUEUED", "disk.ata_error", "hardware", "critical"),
        ("EXT4-fs error (device sda1): ext4_find_entry:1455: inode #2", "fs.error", "server_error", "critical"),
        ("md/raid1:md0: Disk failure on sdb1, disabling device.", "raid.degraded", "hardware", "critical"),
        ("EDAC MC0: 1 UE memory read error on CPU_SrcID#0", "mem.ecc_uncorrectable", "hardware", "critical"),
        ("EDAC MC0: 1 CE memory read error on CPU_SrcID#0", "mem.ecc_correctable", "hardware", "warning"),
        ("mce: [Hardware Error]: Machine check events logged", "cpu.mce", "hardware", "critical"),
        ("Out of memory: Killed process 1234 (java)", "mem.oom", "server_error", "critical"),
        ("INFO: task jbd2/sda1-8:312 blocked for more than 120 seconds.", "kernel.panic", "server_error", "critical"),
        ("python3[999]: segfault at 0 ip 00007f sp 00007ffd error 4", "proc.segfault", "server_error", "warning"),
        ("Failed to start MySQL Server.", "service.failed_log", "server_error", "warning"),
        # lines seen on a real server (nrpe.service, sudo)
        ("Warning: Could not set UID=992", "proc.privilege_drop", "server_error", "warning"),
        ("Warning: Could not set effective GID=988", "proc.privilege_drop", "server_error", "warning"),
        (
            "Warning: Unable to change supplementary groups using initgroups()",
            "proc.privilege_drop",
            "server_error",
            "warning",
        ),
        (
            "freecoupon : user NOT in sudoers ; TTY=pts/0 ; PWD=/home/freecoupon ; USER=root ; "
            "COMMAND=/sbin/usermod -aG systemd-journal freecoupon",
            "auth.sudo_denied",
            "server_error",
            "critical",
        ),
    ],
)
def test_rule_matching(engine, message, rule_id, category, severity):
    f = engine.analyze(1, RawRecord(source="journal", message=message, priority=3))
    assert f is not None
    assert (f.rule_id, f.category, f.severity) == (rule_id, category, severity)


def test_ecc_ce_does_not_match_unrelated_words(engine):
    f = engine.analyze(1, RawRecord(source="file:/var/log/syslog", message="EDAC device registered"))
    assert f is None


def test_systemd_source_rule(engine):
    f = engine.analyze(1, RawRecord(source="systemd", message="nginx.service failed (web)", unit="nginx.service"))
    assert f.rule_id == "service.failed"


def test_unmatched_journal_falls_back_to_priority(engine):
    f = engine.analyze(1, RawRecord(source="journal", message="something odd", priority=2, unit="app"))
    assert (f.rule_id, f.severity) == ("log.priority", "critical")
    assert engine.analyze(1, RawRecord(source="journal", message="hello", priority=6)) is None
    # plain log files have no priority: unmatched lines are ignored
    assert engine.analyze(1, RawRecord(source="file:/var/log/syslog", message="something odd")) is None


def test_every_shipped_rule_is_explained():
    assert [r.id for r in load_rules(settings.rules_dir) if not r.description] == []


def test_rule_file_may_omit_description(tmp_path):
    (tmp_path / "custom.yaml").write_text(
        """
rules:
  - {id: a, title: A, category: hardware, severity: warning, pattern: x, description: 뜻}
  - {id: b, title: B, category: hardware, severity: warning, pattern: y}
""",
        encoding="utf-8",
    )
    assert [r.description for r in load_rules(tmp_path)] == ["뜻", ""]


def test_describe_rule_uses_rule_text_or_a_fallback():
    disk = describe_rule("disk.io_error")
    priority = describe_rule("log.priority")
    unknown = describe_rule("no.such.rule")

    assert "디스크" in disk
    assert "우선순위" in priority
    assert unknown
    assert len({disk, priority, unknown}) == 3


def test_fingerprint_groups_variable_parts():
    a = "blk_update_request: I/O error, dev sda, sector 12345 op 0x0"
    b = "blk_update_request: I/O error, dev sda, sector 99999 op 0x1"
    assert normalize(a) == normalize(b)
    assert fingerprint(1, "r", None, a) == fingerprint(1, "r", None, b)
    assert fingerprint(1, "r", None, a) != fingerprint(2, "r", None, a)
