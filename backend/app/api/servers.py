import asyncssh
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.collector.capabilities import DETECT_SCRIPT, parse_capabilities
from app.collector.ssh_pool import HostKeyChanged, connect, run_command
from app.core.db import get_session
from app.core.security import encrypt
from app.models import Server
from app.schemas import ConnectionTestOut, ServerCreate, ServerOut, ServerUpdate

router = APIRouter(prefix="/api/servers", tags=["servers"])


def to_out(s: Server) -> ServerOut:
    return ServerOut.model_validate(s, from_attributes=True).model_copy(
        update={"host_key_pinned": bool(s.host_key)}
    )


def _validate_secret(auth_type: str, secret: str) -> None:
    if auth_type == "key":
        try:
            asyncssh.import_private_key(secret)
        except (asyncssh.KeyImportError, ValueError) as e:
            raise HTTPException(422, f"invalid private key: {e}") from e


async def _get(session: AsyncSession, server_id: int) -> Server:
    server = await session.get(Server, server_id)
    if server is None:
        raise HTTPException(404, "server not found")
    return server


@router.get("", response_model=list[ServerOut])
async def list_servers(session: AsyncSession = Depends(get_session)):
    servers = (await session.scalars(select(Server).order_by(Server.name))).all()
    return [to_out(s) for s in servers]


@router.post("", response_model=ServerOut, status_code=201)
async def create_server(body: ServerCreate, session: AsyncSession = Depends(get_session)):
    _validate_secret(body.auth_type, body.secret)
    data = body.model_dump(exclude={"secret"})
    server = Server(**data, secret_enc=encrypt(body.secret), capabilities=[], status="unknown")
    session.add(server)
    try:
        await session.commit()
    except IntegrityError as e:
        raise HTTPException(409, "server name already exists") from e
    return to_out(server)


@router.get("/{server_id}", response_model=ServerOut)
async def get_server(server_id: int, session: AsyncSession = Depends(get_session)):
    return to_out(await _get(session, server_id))


@router.patch("/{server_id}", response_model=ServerOut)
async def update_server(
    server_id: int, body: ServerUpdate, session: AsyncSession = Depends(get_session)
):
    server = await _get(session, server_id)
    data = body.model_dump(exclude_unset=True)
    if (secret := data.pop("secret", None)) is not None:
        _validate_secret(data.get("auth_type", server.auth_type), secret)
        server.secret_enc = encrypt(secret)
    if "host" in data and data["host"] != server.host:
        server.host_key = None  # new host: re-pin on next connect
        server.capabilities = []
    for k, v in data.items():
        setattr(server, k, v)
    await session.commit()
    return to_out(server)


@router.delete("/{server_id}", status_code=204)
async def delete_server(server_id: int, session: AsyncSession = Depends(get_session)):
    await session.delete(await _get(session, server_id))
    await session.commit()


@router.post("/{server_id}/test", response_model=ConnectionTestOut)
async def test_connection(server_id: int, session: AsyncSession = Depends(get_session)):
    """Connect, pin the host key on first success, and (re)detect capabilities."""
    server = await _get(session, server_id)
    try:
        conn, host_key = await connect(server)
        async with conn:
            _, out = await run_command(conn, DETECT_SCRIPT)
    except HostKeyChanged as e:
        return ConnectionTestOut(ok=False, error=str(e))
    except Exception as e:  # noqa: BLE001
        return ConnectionTestOut(ok=False, error=f"{type(e).__name__}: {e}")

    if not server.host_key:
        server.host_key = host_key
    server.capabilities = parse_capabilities(out)
    await session.commit()
    return ConnectionTestOut(ok=True, capabilities=server.capabilities, host_key=host_key)


@router.post("/{server_id}/reset-host-key", response_model=ServerOut)
async def reset_host_key(server_id: int, session: AsyncSession = Depends(get_session)):
    """Admin action after a legitimate host key change (e.g. OS reinstall)."""
    server = await _get(session, server_id)
    server.host_key = None
    await session.commit()
    return to_out(server)
