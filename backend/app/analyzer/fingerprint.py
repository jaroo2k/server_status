import hashlib
import re

# Order matters: specific shapes before generic numbers.
_NORMALIZERS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def normalize(message: str) -> str:
    for pattern, repl in _NORMALIZERS:
        message = pattern.sub(repl, message)
    return message.strip()


def fingerprint(server_id: int, rule_id: str, unit: str | None, message: str) -> str:
    key = f"{server_id}|{rule_id}|{unit or ''}|{normalize(message)}"
    return hashlib.sha1(key.encode()).hexdigest()
