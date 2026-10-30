from app.collector.sources.base import CollectResult, RawRecord, Runner, Source


def parse_failed_units(output: str) -> list[RawRecord]:
    records = []
    for line in output.splitlines():
        parts = line.split(None, 4)
        # UNIT LOAD ACTIVE SUB DESCRIPTION
        if len(parts) >= 4 and parts[2] == "failed":
            desc = parts[4] if len(parts) == 5 else ""
            records.append(
                RawRecord(
                    source="systemd",
                    message=f"{parts[0]} failed ({desc})".strip(),
                    unit=parts[0],
                    priority=3,
                )
            )
    return records


class SystemdFailedSource(Source):
    name = "systemd"
    interval = 60
    requires = ("systemctl",)

    async def collect(self, run: Runner, state: dict) -> CollectResult:
        _, out = await run("systemctl --failed --no-legend --plain --no-pager")
        return CollectResult(parse_failed_units(out), {})
