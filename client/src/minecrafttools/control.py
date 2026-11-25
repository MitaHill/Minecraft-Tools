import json
import os
import re
from contextlib import suppress
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from PySide6.QtCore import QObject, QStandardPaths, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest

from minecrafttools.config import TunnelClaim


def control_base() -> str:
    base = os.environ.get("MINECRAFTTOOLS_CONTROL_URL", "").strip().rstrip("/")
    url = QUrl(base)
    local = url.host() in ("localhost", "127.0.0.1", "::1")
    return (
        base
        if (
            url.isValid()
            and url.host()
            and not url.userInfo()
            and not url.hasQuery()
            and not url.hasFragment()
            and (url.scheme() == "https" or (local and url.scheme() == "http"))
        )
        else ""
    )


def valid_token(value) -> bool:
    return (
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is not None
    )


class TunnelControl(QObject):
    def __init__(self, parent: QObject, base: str):
        super().__init__(parent)
        self.base = base
        data_root = os.environ.get(
            "MINECRAFTTOOLS_DATA_ROOT"
        ) or QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation,
        )
        filename = "install-" + sha256(base.encode()).hexdigest() + ".json"
        self.credentials_path = Path(data_root) / filename
        self.token = ""
        if base:
            try:
                stored = json.loads(self.credentials_path.read_text(encoding="utf-8"))
                if isinstance(stored, dict) and valid_token(stored.get("token")):
                    self.token = stored["token"]
            except OSError, ValueError:
                pass
        self.network = QNetworkAccessManager(self)

    def save_install(self, data: dict) -> None:
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with NamedTemporaryFile(
            dir=self.credentials_path.parent, delete=False, mode="w", encoding="utf-8"
        ) as file:
            temp = Path(file.name)
            try:
                json.dump(
                    {"install_id": data["install_id"], "token": data["token"]}, file
                )
                file.close()
                temp.replace(self.credentials_path)
            finally:
                with suppress(FileNotFoundError):
                    temp.unlink()

    def request(
        self, method: bytes, route: str, callback=None, payload=None, *, authorized=True
    ):
        request = QNetworkRequest(QUrl(f"{self.base}/api/v1/{route}"))
        request.setTransferTimeout(7000)
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        if authorized:
            request.setRawHeader(
                b"Authorization", f"Bearer {self.token}".encode("ascii")
            )
        if payload is None:
            reply = self.network.sendCustomRequest(request, method)
        else:
            request.setHeader(
                QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json"
            )
            reply = self.network.sendCustomRequest(
                request, method, json.dumps(payload).encode("utf-8")
            )

        def finished():
            code = (
                reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
            )
            try:
                data = json.loads(reply.readAll().data())
                data = data if isinstance(data, dict) else {}
            except ValueError:
                data = {}
            reply.deleteLater()
            if callback:
                callback(code, data)

        reply.finished.connect(finished)

    def reserve(self, node_id: str, port: int, callback) -> None:
        def enroll():
            self.request(b"POST", "installs/register", enrolled, authorized=False)

        def enrolled(code, data):
            install_id = data.get("install_id")
            if not (
                code == 201
                and valid_token(data.get("token"))
                and isinstance(install_id, str)
                and re.fullmatch(r"[0-9a-f]{32}", install_id)
            ):
                callback(None, "无法注册客户端，请检查控制端连接。")
                return
            try:
                self.save_install(data)
            except OSError:
                callback(None, "无法保存安装身份，请检查用户数据目录。")
                return
            self.token = data["token"]
            request_port(retry=False)

        def reserved(code, data, retry):
            if code == 401 and retry:
                enroll()
                return
            claim_id = data.get("id")
            if (
                code == 201
                and isinstance(claim_id, str)
                and re.fullmatch(r"[0-9a-f]{32}", claim_id)
                and data.get("proxy_name") == f"minecraft-{claim_id}"
                and valid_token(data.get("proof"))
            ):
                callback(TunnelClaim(claim_id, data["proxy_name"], data["proof"]), "")
            elif code == 409:
                callback(None, "远程端口已占用或暂停使用，请选择其他端口。")
            else:
                callback(None, "无法注册隧道，请检查节点配置和控制端连接。")

        def request_port(retry):
            self.request(
                b"POST",
                "tunnels/register",
                lambda code, data: reserved(code, data, retry),
                {"node_id": node_id, "remote_port": port},
            )

        if self.token:
            request_port(retry=True)
        else:
            enroll()

    def status(self, claim: TunnelClaim, renew: bool, callback) -> None:
        suffix = "heartbeat" if renew else "status"
        self.request(
            b"POST" if renew else b"GET", f"tunnels/{claim.id}/{suffix}", callback
        )

    def release(self, claim: TunnelClaim) -> None:
        self.request(b"DELETE", f"tunnels/{claim.id}")
