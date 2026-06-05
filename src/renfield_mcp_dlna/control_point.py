"""ControlPoint: owns the shared UPnP infrastructure and the session registry.

Replaces the module-level globals that previously lived in queue_manager (the
requester / notify server / event handler / factory / sessions dict). One object
owns them so:

  * they can be constructed fresh in tests instead of monkeypatching module state,
  * the lazy-init race (two concurrent first-plays double-binding the notify
    socket) is closed with a lock (code review issue #2),
  * there is a single home for the SSDP listener, device registry, and backend
    factory as later phases land.

ControlPoint deliberately knows nothing about QueueSession — it stores opaque
session objects in `sessions` and exposes the infra (factory/event_handler) that
backends need. Orchestration (building + starting sessions) stays in
queue_manager, which avoids a circular import.
"""

import asyncio
import logging
import os
import socket

from async_upnp_client.aiohttp import AiohttpNotifyServer, AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.event_handler import UpnpEventHandler

logger = logging.getLogger(__name__)


def detect_local_ip() -> str:
    """Detect a local IP that can reach the LAN (for the UPnP callback URL).

    Honours DLNA_LISTEN_IP when the auto-detected interface is wrong (e.g. a
    multi-homed / Docker host picking the wrong NIC).
    """
    env_ip = os.environ.get("DLNA_LISTEN_IP")
    if env_ip:
        return env_ip
    # Connect to the SSDP multicast address to learn our outbound interface.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("239.255.255.250", 1900))
        return s.getsockname()[0]
    except Exception:
        return "0.0.0.0"
    finally:
        s.close()


class ControlPoint:
    """Owns shared UPnP event infrastructure and the per-renderer session map."""

    def __init__(self) -> None:
        self._requester: AiohttpRequester | None = None
        self._notify_server: AiohttpNotifyServer | None = None
        self.event_handler: UpnpEventHandler | None = None
        self.factory: UpnpFactory | None = None
        # renderer UDN → session object (opaque; QueueSession in practice).
        self.sessions: dict[str, object] = {}
        # Serialises lazy infra startup so two concurrent first-plays don't both
        # build and bind a notify server.
        self._infra_lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self._notify_server is not None

    async def ensure_started(self) -> None:
        """Start the shared notify server + event handler (idempotent, race-safe)."""
        if self._notify_server is not None:
            return
        async with self._infra_lock:
            # Re-check under the lock: another coroutine may have started it
            # while we awaited the lock.
            if self._notify_server is not None:
                return
            source_ip = detect_local_ip()
            self._requester = AiohttpRequester()
            self._notify_server = AiohttpNotifyServer(
                requester=self._requester,
                source=(source_ip, 0),  # OS picks a free port
            )
            self.event_handler = UpnpEventHandler(self._notify_server, self._requester)
            self.factory = UpnpFactory(self._requester)
            await self._notify_server.async_start_server()
            logger.info(
                f"UPnP notify server started on {source_ip}, "
                f"callback: {self._notify_server.callback_url}"
            )

    async def aclose(self) -> None:
        """Stop the notify server and drop the infra (idempotent)."""
        if self._notify_server:
            await self._notify_server.async_stop_server()
            logger.info("UPnP notify server stopped")
        self._notify_server = None
        self._requester = None
        self.event_handler = None
        self.factory = None

    # -- session registry --------------------------------------------------

    def register(self, udn: str, session: object) -> None:
        self.sessions[udn] = session

    def get_session(self, udn: str) -> object | None:
        return self.sessions.get(udn)

    def get_all_sessions(self) -> dict[str, object]:
        return dict(self.sessions)

    async def unregister(self, udn: str) -> None:
        """Drop a session; shut the infra down when the last one leaves."""
        self.sessions.pop(udn, None)
        if not self.sessions:
            await self.aclose()
