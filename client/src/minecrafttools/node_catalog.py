import re
from dataclasses import dataclass

NODE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
VERSION = re.compile(r"[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}")


def version_tuple(value: str) -> tuple[int, int, int]:
    if not VERSION.fullmatch(value):
        raise ValueError("Invalid frpc version")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


@dataclass(frozen=True)
class NodeCatalog:
    nodes: tuple[tuple[str, str], ...]
    updated_at: int
    min_frpc_version: str

    @classmethod
    def from_payload(cls, payload: dict) -> "NodeCatalog":
        if not isinstance(payload, dict) or payload.get("blocked") is not False:
            raise ValueError("Invalid node catalog response")
        rows = payload.get("nodes")
        updated_at = payload.get("updated_at")
        minimum = payload.get("min_frpc_version")
        if (
            not isinstance(rows, list) or type(updated_at) is not int or updated_at < 0
            or not isinstance(minimum, str)
        ):
            raise ValueError("Invalid node catalog response")
        version_tuple(minimum)
        nodes = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid node catalog entry")
            node_id, name = row.get("id"), row.get("name")
            if (
                not isinstance(node_id, str) or not NODE_ID.fullmatch(node_id)
                or node_id in nodes or not isinstance(name, str) or not name.strip()
                or len(name) > 80
            ):
                raise ValueError("Invalid node catalog entry")
            nodes[node_id] = name
        return cls(tuple(nodes.items()), updated_at, minimum)

    def payload(self) -> dict:
        return {
            "blocked": False,
            "nodes": [{"id": node_id, "name": name} for node_id, name in self.nodes],
            "updated_at": self.updated_at,
            "min_frpc_version": self.min_frpc_version,
        }
