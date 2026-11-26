import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Node:
    server_addr: str
    server_port: int
    token: str = field(default="", repr=False)
    public_host: str = ""

    def address(self, port: int) -> str:
        host = self.public_host or self.server_addr
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@dataclass(frozen=True)
class TunnelClaim:
    id: str
    proxy_name: str
    proof: str = field(repr=False)


@dataclass(frozen=True)
class TunnelReservation:
    claim: TunnelClaim
    node: Node
    port: int


def valid_host(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 253:
        return False
    if any(char.isspace() or char in "[]/\\?#@" for char in value):
        return False
    if ":" in value:
        try:
            ipaddress.IPv6Address(value)
        except ValueError:
            return False
    return True


def write_frpc_config(
    path: Path,
    node: Node,
    local_port: int,
    remote_port: int,
    claim: TunnelClaim,
) -> None:
    if not 1024 <= remote_port <= 65535:
        raise ValueError("远程端口无效。")
    quote = json.dumps
    lines = [
        f"serverAddr = {quote(node.server_addr, ensure_ascii=False)}",
        f"serverPort = {node.server_port}",
        'transport.tls.enable = false',
        'log.to = "console"',
        'log.level = "info"',
    ]
    if node.token:
        lines.extend(
            [
                'auth.method = "token"',
                f"auth.token = {quote(node.token, ensure_ascii=False)}",
            ]
        )
    lines.extend(
        [
            "",
            "[[proxies]]",
            f"name = {quote(claim.proxy_name)}",
            'type = "tcp"',
            'localIP = "127.0.0.1"',
            f"localPort = {local_port}",
            f"remotePort = {remote_port}",
            f"metadatas.tunnel_id = {quote(claim.id)}",
            f"metadatas.tunnel_proof = {quote(claim.proof)}",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
