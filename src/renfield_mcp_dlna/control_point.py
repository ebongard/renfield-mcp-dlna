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

# Coalesce a burst of SSDP NOTIFYs (devices emit several on boot) into one
# discovery refresh, fired this long after the last change.
_SSDP_REFRESH_DEBOUNCE = 3.0
# How often the watchdog re-polls each active session's transport state. Keeps
# status fresh on event-silent renderers and surfaces a device that vanished;
# GENA subscription renewal itself is handled by async_upnp_client's
# auto_resubscribe, so this stays a READ-ONLY poll (never touches the queue).
_WATCHDOG_INTERVAL = 30.0


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
        # Per-renderer locks so two concurrent play requests on one renderer
        # don't race the stop-old/start-new session swap.
        self._udn_locks: dict[str, asyncio.Lock] = {}
        # Passive SSDP listener + debounced "device set changed" refresh.
        self._ssdp_listener = None
        self._on_change = None
        self._refresh_task: asyncio.Task | None = None
        # Periodic read-only session state watchdog.
        self._watchdog_task: asyncio.Task | None = None

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

    async def stop_background_tasks(self) -> None:
        """Stop the SSDP listener, debounce timer, and watchdog (idempotent).

        Separate from aclose() because the notify-server infra is torn down when
        the last session leaves, whereas the listener/watchdog live for the whole
        process and are stopped only at shutdown.
        """
        for task in (self._refresh_task, self._watchdog_task):
            if task and not task.done():
                task.cancel()
        self._refresh_task = None
        self._watchdog_task = None
        if self._ssdp_listener is not None:
            try:
                await self._ssdp_listener.async_stop()
            except Exception as e:  # noqa: BLE001 - shutdown is best-effort
                logger.debug(f"SSDP listener stop failed: {e}")
            self._ssdp_listener = None
            logger.info("SSDP listener stopped")

    def lock_for(self, udn: str) -> asyncio.Lock:
        """A stable per-renderer lock (created on first use). Held around the
        session-swap critical section in play_tracks."""
        lock = self._udn_locks.get(udn)
        if lock is None:
            lock = asyncio.Lock()
            self._udn_locks[udn] = lock
        return lock

    # -- passive SSDP listener (live device cache) -------------------------

    async def start_discovery_listener(self, on_change) -> None:
        """Listen for SSDP alive/byebye and call `on_change` (an async no-arg
        callable) when the device set changes, debounced. `on_change` typically
        refreshes the discovery caches. Idempotent.

        Intended for the long-lived streamable-http transport; stdio uses
        on-demand search instead (a short-lived subprocess gains little from a
        persistent multicast listener).
        """
        if self._ssdp_listener is not None:
            return
        from async_upnp_client.const import SsdpSource
        from async_upnp_client.ssdp_listener import SsdpListener

        self._on_change = on_change
        # alive/byebye/changed all mean "the device set may have changed".
        relevant = {
            SsdpSource.ADVERTISEMENT_ALIVE,
            SsdpSource.ADVERTISEMENT_BYEBYE,
            SsdpSource.ADVERTISEMENT_UPDATE,
            SsdpSource.SEARCH_ALIVE,
            SsdpSource.SEARCH_CHANGED,
        }

        async def _callback(device, change, source) -> None:
            try:
                if source in relevant:
                    self._schedule_refresh()
            except Exception as e:  # noqa: BLE001 - never raise into the library
                logger.debug(f"SSDP callback error: {e}")

        self._ssdp_listener = SsdpListener(async_callback=_callback)
        await self._ssdp_listener.async_start()
        logger.info("SSDP listener started (live device cache)")

    def _schedule_refresh(self) -> None:
        """(Re)arm the debounce timer; the actual refresh fires once quiet."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = asyncio.ensure_future(self._debounced_refresh())

    async def _debounced_refresh(self) -> None:
        try:
            await asyncio.sleep(_SSDP_REFRESH_DEBOUNCE)
            if self._on_change is not None:
                await self._on_change()
        except asyncio.CancelledError:
            pass  # superseded by a newer change
        except Exception as e:  # noqa: BLE001 - refresh is best-effort
            logger.debug(f"Discovery refresh failed: {e}")

    # -- session watchdog (read-only state refresh) ------------------------

    async def start_session_watchdog(self, interval: float = _WATCHDOG_INTERVAL) -> None:
        """Periodically refresh each active session's transport state (read-only).
        Idempotent. Safe: only calls refresh_state(), never queue operations."""
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.ensure_future(self._watchdog_loop(interval))

    async def _watchdog_loop(self, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                for session in list(self.sessions.values()):
                    refresh = getattr(session, "refresh_state", None)
                    if refresh is None:
                        continue
                    try:
                        await refresh()
                    except Exception as e:  # noqa: BLE001 - per-session best-effort
                        logger.debug(f"Watchdog refresh failed: {e}")
        except asyncio.CancelledError:
            pass

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
