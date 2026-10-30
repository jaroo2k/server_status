from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, Protocol


@dataclass
class RawRecord:
    source: str
    message: str
    ts: datetime | None = None
    # syslog priority (0=emerg .. 7=debug) when the source provides one
    priority: int | None = None
    unit: str | None = None


@dataclass
class CollectResult:
    records: list[RawRecord] = field(default_factory=list)
    new_state: dict = field(default_factory=dict)


class Runner(Protocol):
    async def __call__(self, command: str) -> tuple[int, str]: ...


class Source:
    """A log source. Commands are fixed in code (whitelist); only shlex-quoted
    state values (cursor, offset) are interpolated."""

    name: ClassVar[str]
    interval: ClassVar[int]  # seconds
    # capability token(s) required, see collector.capabilities.DETECT_SCRIPT
    requires: ClassVar[tuple[str, ...]]

    def enabled_for(self, capabilities: list[str]) -> bool:
        return all(r in capabilities for r in self.requires)

    async def collect(self, run: Runner, state: dict) -> CollectResult:
        raise NotImplementedError
