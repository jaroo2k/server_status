import asyncio
import logging

import asyncssh

from app.collector.sources.base import CommandResult
from app.core.config import settings
from app.core.security import decrypt
from app.models import Server

log = logging.getLogger(__name__)


class HostKeyChanged(Exception):
    pass


async def connect(server: Server) -> tuple[asyncssh.SSHClientConnection, str]:
    """Connect with trust-on-first-use host key pinning.
    Returns (connection, host_key_openssh)."""
    secret = decrypt(server.secret_enc)
    kwargs: dict = {
        "host": server.host,
        "port": server.port,
        "username": server.username,
        "connect_timeout": settings.ssh_connect_timeout,
        "keepalive_interval": 30,
        "agent_path": None,
    }
    if server.auth_type == "key":
        kwargs["client_keys"] = [asyncssh.import_private_key(secret)]
        kwargs["password"] = None
    else:
        kwargs["client_keys"] = None
        kwargs["password"] = secret

    if server.host_key:
        kwargs["known_hosts"] = ([asyncssh.import_public_key(server.host_key)], [], [])
    else:
        kwargs["known_hosts"] = None

    try:
        conn = await asyncssh.connect(**kwargs)
    except asyncssh.HostKeyNotVerifiable as e:
        raise HostKeyChanged(f"host key mismatch for {server.host}: {e}") from e

    host_key = conn.get_server_host_key().export_public_key().decode().strip()
    return conn, host_key


class SSHPool:
    """Keeps one connection per server and reuses it across collection cycles."""

    def __init__(self) -> None:
        self._conns: dict[int, asyncssh.SSHClientConnection] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def get(self, server: Server) -> tuple[asyncssh.SSHClientConnection, str | None]:
        lock = self._locks.setdefault(server.id, asyncio.Lock())
        async with lock:
            conn = self._conns.get(server.id)
            if conn is not None and not conn.is_closed():
                return conn, None
            conn, host_key = await connect(server)
            self._conns[server.id] = conn
            return conn, host_key

    def drop(self, server_id: int) -> None:
        if conn := self._conns.pop(server_id, None):
            conn.close()

    def close_all(self) -> None:
        for sid in list(self._conns):
            self.drop(sid)


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


async def run_command(conn: asyncssh.SSHClientConnection, command: str) -> CommandResult:
    result = await asyncio.wait_for(
        conn.run(command, check=False), timeout=settings.ssh_command_timeout
    )
    # exit_status is None when the remote command was killed by a signal
    exit_status = -1 if result.exit_status is None else result.exit_status
    return CommandResult(exit_status, _text(result.stdout), _text(result.stderr))
