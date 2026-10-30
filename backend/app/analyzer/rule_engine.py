import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.analyzer.fingerprint import fingerprint
from app.collector.sources.base import RawRecord


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    category: str
    severity: str
    pattern: re.Pattern
    sources: tuple[str, ...] = ()

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
                )
            )
    return rules


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
