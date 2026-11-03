"""Decides what is worth alerting on and renders it. No I/O."""

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

# Telegram allows 4096; stay below it because it doesn't count length as Python does.
MAX_MESSAGE_CHARS = 4000
MAX_FINDINGS = 10
MAX_DETAIL_CHARS = 300

KST = timezone(timedelta(hours=9))
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
SEVERITY_LABEL = {"critical": "🔴 심각", "warning": "🟡 경고", "info": "🔵 정보"}


def should_alert(severity: str, previous_acknowledged: bool | None, min_severity: str) -> bool:
    """previous_acknowledged is None for a new event. An event still open (False) has
    already alerted; acknowledging it re-arms the alert for its next occurrence."""
    if SEVERITY_RANK.get(severity, 0) < SEVERITY_RANK[min_severity]:
        return False
    return previous_acknowledged is not False


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe_status_change(previous: str, current: str, error: str | None) -> str | None:
    if current == previous:
        return None
    if current == "unreachable":
        return f"🔴 접속 불가\n{_clip(error or '원인 미상', MAX_DETAIL_CHARS)}"
    if previous == "unreachable":
        return "🟢 접속 복구"
    return None


def _describe_finding(finding) -> str:
    label = SEVERITY_LABEL.get(finding.severity, finding.severity)
    detail = _clip(finding.message, MAX_DETAIL_CHARS)
    return f"{label}: {finding.title} ({finding.source})\n{detail}"


def notification_text(
    server_name: str,
    findings: Sequence,
    previous_status: str,
    current_status: str,
    error: str | None,
    now: datetime,
) -> str | None:
    """One message for one collection of one server, or None if nothing to report."""
    # status first: it's the most important line and must survive the length limit
    change = _describe_status_change(previous_status, current_status, error)
    parts = [change] if change else []
    parts += [_describe_finding(f) for f in findings[:MAX_FINDINGS]]
    if not parts:
        return None
    if len(findings) > MAX_FINDINGS:
        parts.append(f"외 {len(findings) - MAX_FINDINGS}건")
    header = f"[{server_name}] {now.astimezone(KST):%Y-%m-%d %H:%M} KST"
    return _clip("\n\n".join([header, *parts]), MAX_MESSAGE_CHARS)
