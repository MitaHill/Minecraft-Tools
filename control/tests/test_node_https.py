import unittest
from hashlib import sha256
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from support import seed_node

from app.main import app


class NodeHTTPSTests(unittest.TestCase):
    def setUp(self):
        data_dir = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {
            "CONTROL_DATA_DIR": data_dir,
            "CONTROL_NODE_HUB_HTTPS": "1",
            "CONTROL_NODE_HUB_PORT": "0",
        }))
        self.client = self.enterContext(TestClient(app))
        self.endpoint = "wss://testserver/api/v1/node/connect"
        self.hello = {
            "type": "hello", "seq": 1, "payload": {
                "protocol_version": 1,
                "node_id": "https-node",
                "secret": "https-test-secret",
                "agent_version": "test",
                "frps_version": "test",
            },
        }
        with app.state.database.begin() as db:
            seed_node(
                db,
                "https-node",
                name="HTTPS test",
                public_host="games.example.test",
                token_hash=sha256(b"https-test-secret").hexdigest(),
            )

    def test_https_node_enrolls_and_sends_snapshot_and_heartbeat(self):
        with self.client.websocket_connect(self.endpoint) as socket:
            for message in (
                self.hello,
                {"type": "tunnel_snapshot", "seq": 2, "payload": {"tunnels": []}},
                {"type": "heartbeat", "seq": 3, "payload": {}},
            ):
                socket.send_json(message)
                reply = socket.receive_json()
                self.assertEqual(reply["type"], message["type"] + "_ack")
                self.assertEqual(reply["seq"], message["seq"])

    def test_declines_cleartext_socket_and_invalid_node_secret(self):
        with (
            self.assertRaises(WebSocketDisconnect),
            self.client.websocket_connect(self.endpoint.replace("wss:", "ws:")),
        ):
            self.fail("accepted a cleartext WebSocket")
        self.hello["payload"]["secret"] = "invalid"
        with self.client.websocket_connect(self.endpoint) as socket:
            socket.send_json(self.hello)
            with self.assertRaises(WebSocketDisconnect):
                socket.receive_json()
