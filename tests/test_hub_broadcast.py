"""Hub.broadcast must survive clients joining or leaving mid-broadcast (issue #8)."""

from __future__ import annotations

from typing import Any

from delta.api.server import Hub


class _FakeWS:
    def __init__(self, hub: Hub, on_send: Any = None, fail: bool = False) -> None:
        self.hub = hub
        self.on_send = on_send
        self.fail = fail
        self.got: list[dict[str, Any]] = []

    async def send_json(self, message: dict[str, Any]) -> None:
        if self.on_send:
            self.on_send(self)
        if self.fail:
            raise RuntimeError("socket closed")
        self.got.append(message)


async def test_client_joining_mid_broadcast_does_not_crash() -> None:
    hub = Hub()
    newcomer = _FakeWS(hub)
    first = _FakeWS(hub, on_send=lambda ws: ws.hub.clients.add(newcomer))
    second = _FakeWS(hub)
    hub.clients.update({first, second})  # type: ignore[arg-type]

    await hub.broadcast({"type": "event"})

    assert first.got == [{"type": "event"}]
    assert second.got == [{"type": "event"}]
    assert newcomer in hub.clients


async def test_client_leaving_mid_broadcast_does_not_crash() -> None:
    hub = Hub()
    other = _FakeWS(hub)
    leaver = _FakeWS(hub, on_send=lambda ws: ws.hub.clients.discard(other))
    hub.clients.update({leaver, other})  # type: ignore[arg-type]

    await hub.broadcast({"type": "event"})

    assert leaver.got == [{"type": "event"}]


async def test_dead_clients_are_dropped() -> None:
    hub = Hub()
    alive = _FakeWS(hub)
    dead = _FakeWS(hub, fail=True)
    hub.clients.update({alive, dead})  # type: ignore[arg-type]

    await hub.broadcast({"type": "event"})

    assert alive.got == [{"type": "event"}]
    assert dead not in hub.clients
