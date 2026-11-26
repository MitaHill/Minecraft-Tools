from sqlalchemy import insert

from app.node_ports import node_port_ranges
from app.registry import nodes


def seed_node(connection, node_id, ranges=((30001, 30002),), **overrides):
    values = {
        "id": node_id,
        "name": node_id,
        "public_host": f"{node_id}.example",
        "status": "online",
        "token_hash": "fixture",
    }
    values.update(overrides)
    connection.execute(insert(nodes).values(**values))
    connection.execute(
        insert(node_port_ranges),
        [
            {"node_id": node_id, "pending": False, "start_port": start, "end_port": end}
            for start, end in ranges
        ],
    )
