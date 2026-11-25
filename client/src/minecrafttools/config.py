import ipaddress
import json
import re
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Node:
    name: str
    server_addr: str
    server_port: int
    token: str = ""
    public_host: str = ""
    remote_port_start: int | None = None
    remote_port_end: int | None = None
    node_id: str = ""

    def address(self, port: int) -> str:
        host = self.public_host or self.server_addr
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"

    @property
    def port_range(self) -> tuple[int, int]:
        if self.remote_port_start is None or self.remote_port_end is None:
            return 1, 65535
        return self.remote_port_start, self.remote_port_end


@dataclass(frozen=True)
class TunnelClaim:
    id: str
    proxy_name: str
    proof: str = field(repr=False)


def load_nodes(path: Path) -> list[Node]:
    with path.open("rb") as file:
        items = tomllib.load(file).get("nodes", [])
    if not isinstance(items, list) or not items:
        raise ValueError("节点配置需要至少一个 [[nodes]]。")

    nodes = []
    names = set()
    for index, item in enumerate(items, start=1):
        try:
            node = Node(**item)
            if not all(
                isinstance(value, str)
                for value in (
                    node.name,
                    node.server_addr,
                    node.token,
                    node.public_host,
                    node.node_id,
                )
            ):
                raise ValueError
            if not node.name.strip() or node.name in names:
                raise ValueError
            if type(node.server_port) is not int or not 1 <= node.server_port <= 65535:
                raise ValueError
            start, end = node.remote_port_start, node.remote_port_end
            if (start is not None or end is not None) and (
                type(start) is not int
                or type(end) is not int
                or not 1024 <= start <= end <= 65535
            ):
                raise ValueError
            if node.node_id and (
                not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", node.node_id)
                or start is None
            ):
                raise ValueError
            for host in (node.server_addr, node.public_host or node.server_addr):
                if not host or any(
                    char.isspace() or char in "[]/\\?#" for char in host
                ):
                    raise ValueError
                if ":" in host:
                    ipaddress.IPv6Address(host)
        except TypeError, ValueError:
            raise ValueError(
                f"第 {index} 个节点配置无效，请检查字段、端口和重复名称。"
            ) from None
        nodes.append(node)
        names.add(node.name)
    return nodes


def write_frpc_config(
    path: Path,
    node: Node,
    local_port: int,
    remote_port: int,
    claim: TunnelClaim | None = None,
) -> None:
    start, end = node.port_range
    if not start <= remote_port <= end:
        raise ValueError("远程端口超出节点允许的范围。")
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
    proxy_name = claim.proxy_name if claim else f"minecraft-{uuid.uuid4().hex[:12]}"
    lines.extend(
        [
            "",
            "[[proxies]]",
            f"name = {quote(proxy_name)}",
            'type = "tcp"',
            'localIP = "127.0.0.1"',
            f"localPort = {local_port}",
            f"remotePort = {remote_port}",
        ]
    )
    if claim:
        lines.extend(
            [
                f"metadatas.tunnel_id = {quote(claim.id)}",
                f"metadatas.tunnel_proof = {quote(claim.proof)}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
