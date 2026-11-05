from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.analyzer.rule_engine import describe_rule


class ServerCreate(BaseModel):
    name: str = Field(max_length=100)
    host: str
    port: int = 22
    username: str = "monitor"
    auth_type: Literal["key", "password"] = "key"
    secret: str = Field(description="OpenSSH private key text or password")
    tags: list[str] = []
    enabled: bool = True


class ServerUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    auth_type: Literal["key", "password"] | None = None
    secret: str | None = None
    tags: list[str] | None = None
    enabled: bool | None = None


class ServerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    host: str
    port: int
    username: str
    auth_type: str
    tags: list[str]
    enabled: bool
    capabilities: list[str]
    status: str
    host_key_pinned: bool = False
    consecutive_failures: int
    last_collected_at: datetime | None
    last_error: str | None


class ConnectionTestOut(BaseModel):
    ok: bool
    capabilities: list[str] = []
    host_key: str | None = None
    error: str | None = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    server_id: int
    server_name: str | None = None
    source: str
    rule_id: str
    category: str
    severity: str
    title: str
    message: str
    count: int
    first_seen: datetime
    last_seen: datetime
    acknowledged: bool

    @computed_field
    @property
    def description(self) -> str:
        return describe_rule(self.rule_id)


class OccurrenceOut(BaseModel):
    id: int
    event_id: int
    server_id: int
    server_name: str
    source: str
    rule_id: str
    severity: str
    title: str
    message: str
    seen_at: datetime


class OccurrencePage(BaseModel):
    """`total` counts lines matching the filters, not just this page. If `total_capped`,
    counting stopped there and more lines match."""

    total: int
    total_capped: bool
    items: list[OccurrenceOut]


class SourceStatusOut(BaseModel):
    """Latest run of one log source. `ok` is None until the source has run."""

    model_config = ConfigDict(from_attributes=True)

    source: str
    ok: bool | None = None
    exit_status: int | None = None
    error: str | None = None
    command: str | None = None
    records: int = 0
    findings: int = 0
    duration_ms: int = 0
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None


class Summary(BaseModel):
    servers_total: int
    servers_by_status: dict[str, int]
    open_events_by_severity: dict[str, int]
    open_events_by_category: dict[str, int]
