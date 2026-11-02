from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.collector.sources.base import CommandResult

MAX_ERROR_CHARS = 2000


@dataclass
class CommandFailure:
    command: str
    exit_status: int
    error: str


def _describe(exit_status: int, stderr: str) -> str:
    detail = stderr.replace("\x00", "").strip() or "(no stderr)"
    return f"exit {exit_status}: {detail}"[:MAX_ERROR_CHARS]


class TrackingRunner:
    """Runner handed to a source for one run. Sources only see (exit, stdout);
    this keeps what they don't look at, so a nonzero exit can't pass unnoticed."""

    def __init__(self, execute: Callable[[str], Awaitable[CommandResult]]):
        self._execute = execute
        self.failure: CommandFailure | None = None
        self.last_command: str | None = None

    async def __call__(self, command: str) -> tuple[int, str]:
        self.last_command = command
        result = await self._execute(command)
        if result.exit_status != 0 and self.failure is None:
            self.failure = CommandFailure(
                command, result.exit_status, _describe(result.exit_status, result.stderr)
            )
        return result.exit_status, result.stdout
