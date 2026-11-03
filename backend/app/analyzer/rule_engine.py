import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from app.analyzer.fingerprint import fingerprint
from app.collector.sources.base import RawRecord
from app.core.config import settings


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    category: str
    severity: str
    pattern: re.Pattern
    sources: tuple[str, ...] = ()
    description: str = ""

    def matches(self, rec: RawRecord) -> bool:
        if self.sources and not any(rec.source.startswith(s) for s in self.sources):
            return False
        return bool(self.pattern.search(rec.message))


@dataclass
class Finding:
    rule_id: str
    title: str
    category: str
    severity: str
    source: str
    message: str
    fingerprint: str
    record: RawRecord


SEVERITIES = ("critical", "warning", "info")


def load_rules(rules_dir: Path) -> list[Rule]:
    rules: list[Rule] = []
    for path in sorted(rules_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for r in data.get("rules", []):
            if r["severity"] not in SEVERITIES:
                raise ValueError(f"{path}: rule {r['id']} has invalid severity")
            rules.append(
                Rule(
                    id=r["id"],
                    title=r["title"],
                    category=r["category"],
                    severity=r["severity"],
                    pattern=re.compile(r["pattern"], re.IGNORECASE),
                    sources=tuple(r.get("sources", ())),
                    description=r.get("description", ""),
                )
            )
    return rules


# "log.priority" is not a rule in the YAML files, see RuleEngine.analyze.
_BUILTIN_DESCRIPTIONS = {
    "log.priority": "탐지 규칙에는 없지만 시스템 저널에 에러 이상의 우선순위로 기록된 로그입니다. "
    "원본 로그에서 어떤 프로그램이 무엇을 실패했는지 확인하세요.",
}
_UNKNOWN_DESCRIPTION = "이 규칙에는 등록된 설명이 없습니다. 원본 로그를 확인하세요."


@lru_cache
def _descriptions() -> dict[str, str]:
    return _BUILTIN_DESCRIPTIONS | {r.id: r.description for r in load_rules(settings.rules_dir)}


def describe_rule(rule_id: str) -> str:
    """What an event of this rule means. Rules are read once, like in the collector."""
    return _descriptions().get(rule_id) or _UNKNOWN_DESCRIPTION


class RuleEngine:
    def __init__(self, rules: list[Rule]):
        self.rules = rules

    def analyze(self, server_id: int, rec: RawRecord) -> Finding | None:
        for rule in self.rules:
            if rule.matches(rec):
                return self._finding(server_id, rec, rule.id, rule.title, rule.category, rule.severity)
        # Unmatched journal errors are still reported, graded by syslog priority.
        if rec.priority is not None and rec.priority <= 3 and rec.source == "journal":
            sev = "critical" if rec.priority <= 2 else "warning"
            title = f"에러 로그 ({rec.unit})" if rec.unit else "에러 로그"
            return self._finding(server_id, rec, "log.priority", title, "server_error", sev)
        return None

    @staticmethod
    def _finding(server_id, rec, rule_id, title, category, severity) -> Finding:
        return Finding(
            rule_id=rule_id,
            title=title[:200],
            category=category,
            severity=severity,
            source=rec.source,
            message=rec.message,
            fingerprint=fingerprint(server_id, rule_id, rec.unit, rec.message),
            record=rec,
        )
