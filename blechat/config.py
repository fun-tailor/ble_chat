from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

MAX_NETWORKS = 32
DEFAULT_HISTORY_DAYS = 3

WIDTH = 700
HEIGHT = 640

_MAC_RE = re.compile(r"([0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5})")


def addr_keys(value: str) -> set[str]:
    """把一个"地址"归一成一组可比对的键。

    `networks.json` 里的 `host_address` 和运行时拿到的可能写法不同 ——
    `8C:C6:…`、`8c-c6-…`、或 WinRT 的设备路径
    `BluetoothLE#BluetoothLE<本地>-<远程>`。以前 `find_by_host` 只做
    `.upper()` 比较，写法一变就**匹配不上** ⇒ 走到"铸新 network_id"分支 ⇒
    历史按新 id 查不到行 ⇒ dev 报「client 历史被清空、可能重新创建」。

    返回所有能抽出来的 6 字节 MAC（含整串十六进制的退化形式），
    比对时取**交集非空**即可。
    """
    text = value or ""
    keys = {m.replace(":", "").replace("-", "").upper() for m in _MAC_RE.findall(text)}
    hex_only = "".join(ch for ch in text if ch in "0123456789abcdefABCDEF").upper()
    if hex_only:
        keys.add(hex_only)
    return keys


def app_root() -> Path:
    """持久化目录：exe 同级（打包后）/ 项目根目录（源码运行）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


@dataclass
class WindowGeom:
    w: int = WIDTH
    h: int = HEIGHT
    x: int | None = None
    y: int | None = None


@dataclass
class Config:
    history_days: int = DEFAULT_HISTORY_DAYS
    last_network_id: str | None = None
    theme: str = "auto"
    mode: str = "host"
    # Windows 上 PairAsync 通常 30s 后 FAILED 并把链路一起断掉（见 PROTOCOL.md §8），
    # 因此默认不发起系统配对；本应用自身用 PSK 做端到端加密。
    system_pairing: bool = False
    # 勾选"记住密码"时把 PSK 用 DPAPI 加密后落盘（keys/credentials.json）
    persist_credentials: bool = True
    enable_emoji: bool = True
    receive_dir: str = ""
    auto_save_files: bool = False
    # "INFO"（默认）或 "DEBUG"。真机排查（连接反复失败、历史看着被清空）时
    # 改成 "DEBUG" 重启即可拿到逐次连接/端口自愈/历史选取的详细轨迹；
    # 环境变量 BLECHAT_LOG_LEVEL 优先级更高，不用改文件。
    log_level: str = "INFO"
    window: WindowGeom = field(default_factory=WindowGeom)
    path: Path = field(default_factory=lambda: app_root() / "config.json", repr=False)
    # 日志记录开关
    log_file: bool = False

    @classmethod
    def load(cls, root: Path | None = None) -> Config:
        root = root or app_root()
        raw = read_json(root / "config.json", {})
        geom = raw.get("window") or {}
        mode = str(raw.get("mode", "host")).lower()
        return cls(
            history_days=int(raw.get("history_days", DEFAULT_HISTORY_DAYS)),
            last_network_id=raw.get("last_network_id"),
            theme=str(raw.get("theme", "auto")),
            mode=mode if mode in ("host", "join") else "host",
            system_pairing=bool(raw.get("system_pairing", False)),
            persist_credentials=bool(raw.get("persist_credentials", True)),
            enable_emoji=bool(raw.get("enable_emoji", False)),
            receive_dir=str(raw.get("receive_dir", "")),
            auto_save_files=bool(raw.get("auto_save_files", False)),
            log_level=str(raw.get("log_level", "INFO")),
            log_file = bool(raw.get("log_file", False)),
            window=WindowGeom(
                w=int(geom.get("w", WIDTH)),
                h=int(geom.get("h", HEIGHT)),
                x=geom.get("x"),
                y=geom.get("y"),
            ),
            path=root / "config.json",
        )

    def save(self) -> None:
        data = asdict(self)
        data.pop("path", None)
        atomic_write_json(self.path, data)


@dataclass
class Network:
    network_id: str
    host_address: str
    host_name: str
    service_uuid: str
    paired: bool = False
    paired_at: int | None = None
    last_joined_at: int = 0
    auth: dict = field(default_factory=dict)
    # 对端在 HELLO 里广播的昵称（空则回退 host_name / 系统设备名）
    peer_alias: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> Network:
        return cls(
            network_id=str(raw.get("network_id", "")),
            host_address=str(raw.get("host_address", "")),
            host_name=str(raw.get("host_name", "")),
            service_uuid=str(raw.get("service_uuid", "")),
            paired=bool(raw.get("paired", False)),
            paired_at=raw.get("paired_at"),
            last_joined_at=int(raw.get("last_joined_at", 0)),
            auth=dict(raw.get("auth") or {}),
            peer_alias=str(raw.get("peer_alias") or ""),
        )

    def to_dict(self) -> dict:
        return asdict(self)


class NetworksStore:
    """networks.json — 切换时保留历史网络，上限 MAX_NETWORKS。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or app_root()
        self.path = self.root / "networks.json"
        raw = read_json(self.path, {})
        ident = raw.get("identity") or {}
        self.device_id: str = str(ident.get("device_id", ""))
        self.device_name: str = str(ident.get("name", ""))
        self.networks: list[Network] = [Network.from_dict(n) for n in raw.get("networks", [])]

    def save(self) -> None:
        self._evict()
        atomic_write_json(
            self.path,
            {
                "identity": {"device_id": self.device_id, "name": self.device_name},
                "networks": [n.to_dict() for n in self.networks],
            },
        )

    def set_identity(self, device_id: str, name: str) -> None:
        self.device_id = device_id
        self.device_name = name

    def get(self, network_id: str | None) -> Network | None:
        if not network_id:
            return None
        for net in self.networks:
            if net.network_id == network_id:
                return net
        return None

    def find_by_host(self, host_address: str, service_uuid: str) -> Network | None:
        # 先按老写法精确比一次（保持原行为），再按归一化的 MAC 集合比一次。
        for net in self.networks:
            if net.host_address.upper() == host_address.upper() and net.service_uuid == service_uuid:
                return net
        want = addr_keys(host_address)
        if not want:
            return None
        for net in self.networks:
            if net.service_uuid != service_uuid:
                continue
            if addr_keys(net.host_address) & want:
                return net
        return None

    def upsert(self, net: Network) -> None:
        for i, existing in enumerate(self.networks):
            if existing.network_id == net.network_id:
                self.networks[i] = net
                break
        else:
            self.networks.append(net)
        self._evict()

    def _evict(self) -> None:
        if len(self.networks) <= MAX_NETWORKS:
            return
        self.networks.sort(key=lambda n: n.last_joined_at, reverse=True)
        del self.networks[MAX_NETWORKS:]
