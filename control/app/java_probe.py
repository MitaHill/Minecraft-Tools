import asyncio
import json
import socket
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Literal

ProbeResult = Literal["ok", "not_minecraft", "unreachable", "unknown"]
MAX_STATUS_BYTES = 65536


@dataclass(frozen=True)
class Status:
    result: ProbeResult
    latency_ms: int | None = None
    version: str | None = None
    motd: str | None = None
    error: str | None = None


def encode_length(number: int) -> bytes:
    result = bytearray()
    while number >= 128:
        result.append((number & 127) | 128)
        number >>= 7
    result.append(number)
    return bytes(result)


async def read_length(reader: asyncio.StreamReader) -> int:
    result = 0
    for shift in range(0, 35, 7):
        byte = (await reader.readexactly(1))[0]
        result |= (byte & 127) << shift
        if byte < 128:
            return result
    raise ValueError("Invalid VarInt")


@asynccontextmanager
async def connect(address: str, port: int):
    reader, writer = await asyncio.open_connection(address, port)
    try:
        yield reader, writer
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


def motd_text(value: object, depth: int = 0) -> str:
    if depth >= 6:
        return ""
    if isinstance(value, str):
        return value[:300]
    if isinstance(value, list):
        return "".join(motd_text(item, depth + 1) for item in value[:32])[:300]
    if isinstance(value, dict):
        return (
            motd_text(value.get("text"), depth + 1)
            + motd_text(value.get("extra"), depth + 1)
        )[:300]
    return ""


async def modern_status(address: str, host: str, port: int) -> tuple[str, str]:
    async with connect(address, port) as (reader, writer):
        hostname = host.encode("utf-8")
        hello = (
            b"\x00\x2f"
            + encode_length(len(hostname))
            + hostname
            + port.to_bytes(2, "big")
            + b"\x01"
        )
        writer.write(encode_length(len(hello)) + hello + b"\x01\x00")
        await writer.drain()
        length = await read_length(reader)
        if length < 2 or length > MAX_STATUS_BYTES or await read_length(reader) != 0:
            raise ValueError("Invalid Java status packet")
        payload_size = await read_length(reader)
        if payload_size != length - 1 - len(encode_length(payload_size)):
            raise ValueError("Invalid Java status document length")
        data = json.loads(await reader.readexactly(payload_size))
        if not isinstance(data, dict):
            raise TypeError("Status payload must be an object")
        version, players = data.get("version"), data.get("players")
        if (
            not isinstance(version, dict)
            or not isinstance(version.get("name"), str)
            or type(version.get("protocol")) is not int
            or not isinstance(players, dict)
            or type(players.get("online")) is not int
            or type(players.get("max")) is not int
        ):
            raise ValueError("Status is missing Java version or player fields")
        return version["name"][:100], motd_text(data.get("description"))


async def legacy_status(address: str, port: int) -> tuple[str, str]:
    async with connect(address, port) as (reader, writer):
        writer.write(b"\xfe\x01")
        await writer.drain()
        if await reader.readexactly(1) != b"\xff":
            raise ValueError("Invalid legacy status packet")
        length = int.from_bytes(await reader.readexactly(2), "big") * 2
        if length > MAX_STATUS_BYTES:
            raise ValueError("Legacy status packet too large")
        data = (await reader.readexactly(length)).decode("utf-16-be")
        if data.startswith("§1\x00"):
            parts = data.split("\x00")
            if len(parts) == 6 and all(parts[index].isdigit() for index in (1, 4, 5)):
                return parts[2][:100], parts[3][:300]
        else:
            parts = data.rsplit("§", 2)
            if len(parts) == 3 and all(part.isdigit() for part in parts[1:]):
                return "Legacy Java", parts[0][:300]
        raise ValueError("Invalid legacy status response")


async def probe(
    host: str, port: int, *, allow_private: bool = False, legacy: bool = True
) -> Status:
    loop = asyncio.get_running_loop()
    start = loop.time()
    try:
        async with asyncio.timeout(3):
            records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = dict.fromkeys(record[4][0] for record in records)
        addresses = [
            address
            for address in addresses
            if not ip_address(address).is_multicast
            and (allow_private or ip_address(address).is_global)
        ]
    except (OSError, ValueError):
        return Status("unreachable", error="DNS lookup failed")
    if not addresses:
        return Status("unknown", error="No permitted address for registered node")

    for address in addresses[:3]:
        try:
            async with asyncio.timeout(3):
                version, motd = await modern_status(address, host, port)
        except (TypeError, ValueError, asyncio.IncompleteReadError, RecursionError):
            if not legacy:
                return Status("not_minecraft", error="Malformed Java status response")
            try:
                async with asyncio.timeout(1):
                    version, motd = await legacy_status(address, port)
            except (ValueError, OSError, asyncio.IncompleteReadError):
                return Status("not_minecraft", error="Malformed Java status response")
        except (OSError, TimeoutError):
            continue
        return Status("ok", round((loop.time() - start) * 1000), version, motd)
    return Status("unreachable", error="Connection refused or timed out")
