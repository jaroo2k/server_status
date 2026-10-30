from app.collector.sources.base import Source
from app.collector.sources.dmesg import DmesgSource
from app.collector.sources.journal import JournalSource
from app.collector.sources.logfile import LOG_FILES, LogFileSource
from app.collector.sources.systemd import SystemdFailedSource


def all_sources() -> list[Source]:
    return [
        JournalSource(),
        DmesgSource(),
        SystemdFailedSource(),
        *(LogFileSource(p) for p in LOG_FILES),
    ]


def sources_for(capabilities: list[str]) -> list[Source]:
    srcs = [s for s in all_sources() if s.enabled_for(capabilities)]
    # journald hosts usually mirror syslog files; avoid double-reporting
    if "journalctl" in capabilities:
        srcs = [s for s in srcs if not isinstance(s, LogFileSource)]
    return srcs
