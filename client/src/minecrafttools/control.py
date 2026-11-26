import json
import logging
import os
import re
from contextlib import suppress
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from PySide6.QtCore import QObject, QStandardPaths, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest

from minecrafttools.config import Node, TunnelClaim, TunnelReservation, valid_host
from minecrafttools.node_catalog import NodeCatalog

logger = logging.getLogger(__name__)


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


def blacklist_message(data: dict) -> str:
    detail = data.get("detail")
    if isinstance(detail, dict) and detail.get("code") == "ip_blacklisted":
        reason = detail.get("reason")
        return "客户端 IP 已被封禁。" + (reason if isinstance(reason, str) else "")
    return ""


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as file:
        temp = Path(file.name)
        try:
            json.dump(data, file)
            file.close()
            temp.replace(path)
        finally:
            with suppress(FileNotFoundError):
                temp.unlink()


class TunnelControl(QObject):
    def __init__(self, parent: QObject, base: str):
        super().__init__(parent)
        self.base = base
        data_root = os.environ.get(
            "MINECRAFTTOOLS_DATA_ROOT"
        ) or QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation,
        )
        source = sha256(base.encode()).hexdigest()
        self.credentials_path = Path(data_root) / f"install-{source}.json"
        self.channel = (
            "dev" if os.environ.get("MINECRAFTTOOLS_BUILD_CHANNEL") == "dev" else "stable"
        )
        self.catalog_path = Path(data_root) / f"nodes-{source}-{self.channel}.json"
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
        save_json(self.credentials_path, {"install_id": data["install_id"], "token": data["token"]})

    def cached_catalog(self) -> NodeCatalog | None:
        try:
            return NodeCatalog.from_payload(json.loads(self.catalog_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.warning("无法读取本地线路清单缓存。")
            return None

    def fetch_catalog(self, callback) -> None:
        self.request(b"GET", f"node-catalog?channel={self.channel}", callback, authorized=False)

    def cache_catalog(self, catalog: NodeCatalog) -> None:
        if catalog == self.cached_catalog():
            return
        try:
            save_json(self.catalog_path, catalog.payload())
        except OSError:
            logger.warning("无法保存本地线路清单缓存。")

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

    def reserve(self, node_id: str, callback) -> None:
        def enroll():
            self.request(b"POST", "installs/register", enrolled, authorized=False)

        def denied(data):
            if message := blacklist_message(data):
                callback(None, message)
                return True
            detail = data.get("detail")
            if isinstance(detail, dict):
                messages = {
                    "node_range_pending": "节点正在更新端口范围，请稍后重试。",
                    "node_not_found": "线路已下架，请重新打开软件刷新线路清单。",
                    "node_unconfigured": "此线路尚未完成配置，请换个节点。",
                    "node_offline": "节点当前离线，请换个节点或稍后重试。",
                    "no_available_port": "此节点暂无可用端口，请换个节点或稍后重试。",
                }
                error = detail.get("code")
                if isinstance(error, str) and error in messages:
                    callback(None, messages[error])
                    return True
            return False

        def enrolled(code, data):
            if denied(data):
                return
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
            if denied(data):
                return
            if code == 401 and retry:
                enroll()
                return
            claim_id = data.get("id")
            if not (
                code == 201 and isinstance(claim_id, str) and re.fullmatch(r"[0-9a-f]{32}", claim_id)
                and data.get("proxy_name") == f"minecraft-{claim_id}" and valid_token(data.get("proof"))
            ):
                callback(None, "无法注册隧道，请检查节点配置和控制端连接。")
                return
            claim = TunnelClaim(claim_id, data["proxy_name"], data["proof"])
            port, server_port = data.get("remote_port"), data.get("server_port")
            host, public_host = data.get("server_addr"), data.get("public_host")
            token = data.get("frp_token", "")
            if not (
                type(port) is int and 1024 <= port <= 65535
                and type(server_port) is int and 1 <= server_port <= 65535
                and valid_host(host) and valid_host(public_host) and isinstance(token, str)
            ):
                self.release(claim)
                callback(None, "控制端返回的线路配置无效，请稍后重试。")
                return
            node = Node(host, server_port, token, public_host)
            callback(TunnelReservation(claim, node, port), "")

        def request_port(retry):
            self.request(
                b"POST",
                "tunnels/register",
                lambda code, data: reserved(code, data, retry),
                {"node_id": node_id},
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
