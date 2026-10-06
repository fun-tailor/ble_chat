"""传输层抽象：Host(WinRT GATT server) 与 Client(bleak) 共用同一接口。"""

from __future__ import annotations

from typing import Callable

from ..protocol import chunk_size


class Transport:
    """字节管道。回调可能来自任意线程，实现方需自行切回事件循环。"""

    peer_id: str = ""
    peer_name: str = ""

    on_ctrl: Callable[[bytes], None] | None = None
    on_data: Callable[[bytes], None] | None = None
    on_closed: Callable[[str], None] | None = None

    @property
    def max_chunk(self) -> int:
        return chunk_size(self.mtu)

    mtu: int = 23

    async def send_ctrl(self, data: bytes) -> None:
        raise NotImplementedError

    async def send_data(self, data: bytes) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def pair(self) -> bool:
        return False


class ServerTransport(Transport):
    """Host 侧：把 GattServer 的某一个对端包装成 Transport。"""

    def __init__(self, server, peer_id: str, peer_name: str = "") -> None:
        self._server = server
        self.peer_id = peer_id
        self.peer_name = peer_name

    @property
    def mtu(self) -> int:
        try:
            return int(self._server.peer_mtu(self.peer_id))
        except Exception:
            return 23

    @mtu.setter
    def mtu(self, value: int) -> None:
        pass

    @property
    def max_chunk(self) -> int:
        getter = getattr(self._server, "peer_chunk", None)
        if callable(getter):
            try:
                return int(getter(self.peer_id))
            except Exception:
                pass
        return chunk_size(self.mtu)

    async def send_ctrl(self, data: bytes) -> None:
        await self._server.send_ctrl(self.peer_id, data)

    async def send_data(self, data: bytes) -> None:
        await self._server.send_data(self.peer_id, data)

    async def close(self) -> None:
        self._server.drop_peer(self.peer_id)


class FakeTransport(Transport):
    """测试用内存传输：两条管道直连，可注入延迟/丢包。"""

    def __init__(self, peer_id: str = "fake", peer_name: str = "fake") -> None:
        self.peer_id = peer_id
        self.peer_name = peer_name
        self.mtu = 512
        self.peer: FakeTransport | None = None
        self.loss = 0.0
        self.loss_data = 0.0
        self.delay = 0.0
        self.closed = False
        self.sent_ctrl: list[bytes] = []
        self.sent_data: list[bytes] = []

    @staticmethod
    def pair(a_id: str = "a", b_id: str = "b") -> tuple[FakeTransport, FakeTransport]:
        a = FakeTransport(a_id, "peer-b")
        b = FakeTransport(b_id, "peer-a")
        a.peer, b.peer = b, a
        return a, b

    async def send_ctrl(self, data: bytes) -> None:
        await self._emit(data, control=True)

    async def send_data(self, data: bytes) -> None:
        await self._emit(data, control=False)

    async def _emit(self, data: bytes, *, control: bool) -> None:
        if self.closed or self.peer is None:
            return
        import asyncio
        import random

        if self.delay:
            await asyncio.sleep(self.delay)
        drop = self.loss if control else max(self.loss, self.loss_data)
        if drop and random.random() < drop:
            return
        (self.sent_ctrl if control else self.sent_data).append(data)
        target = self.peer
        cb = target.on_ctrl if control else target.on_data
        if cb:
            cb(bytes(data))

    async def close(self) -> None:
        self.closed = True
        if self.peer and self.peer.on_closed:
            self.peer.on_closed("closed")
