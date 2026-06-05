"""Sonos backend, wrapping the `soco` library.

Sonos speaks UPnP but bends it (the queue lives behind x-rincon URIs with their
own add/clear actions, plus zone grouping). Rather than re-implement those
quirks, this delegates to `soco` (Layer-1: mature, maintained) behind the
PlaybackBackend interface, isolated to this one adapter file.

soco is SYNCHRONOUS, so every call is run in a worker thread (asyncio.to_thread)
to avoid blocking the event loop. The Sonos unit is addressed by the host from
our own SSDP discovery (renderer.location), so we don't run soco's separate
discovery — one device identity, reconciled by host.

soco owns the queue, so owns_queue is True (same integration path as OpenHome).

PROVISIONAL — validated against the soco API shape + mocks only, NOT a real
Sonos. `soco` is an OPTIONAL dependency (`pip install '.[sonos]'`) and the
backend is selected only when RENFIELD_SONOS=1, so it can't affect anyone else.
"""

import asyncio
import logging
from urllib.parse import urlparse

from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.event_handler import UpnpEventHandler

from ..discovery import DlnaRenderer
from .base import PlaybackBackend, TransportEvent

logger = logging.getLogger(__name__)


class SonosBackend(PlaybackBackend):
    """Playback on a Sonos device via the soco library."""

    owns_queue = True

    def __init__(self, renderer: DlnaRenderer):
        self.renderer = renderer
        self._soco = None
        self._on_event: TransportEvent | None = None
        self._current_index = 0
        self._queue_len = 0

    @property
    def supports_next(self) -> bool:
        return True

    @property
    def connected(self) -> bool:
        return self._soco is not None

    @property
    def transport_state(self) -> str | None:
        if self._soco is None:
            return None
        # soco reports PLAYING/PAUSED_PLAYBACK/STOPPED/TRANSITIONING — the same
        # vocabulary the rest of the code already uses.
        info = self._soco.get_current_transport_info()
        return info.get("current_transport_state")

    def _host(self) -> str | None:
        return urlparse(self.renderer.location).hostname

    async def connect(
        self,
        on_event: TransportEvent,
        *,
        factory: UpnpFactory,
        event_handler: UpnpEventHandler,
    ) -> None:
        self._on_event = on_event
        try:
            import soco  # optional dependency
        except ImportError as e:  # pragma: no cover - exercised only without soco
            raise RuntimeError(
                "Sonos support needs the 'soco' package (pip install '.[sonos]')"
            ) from e
        self._soco = await asyncio.to_thread(soco.SoCo, self._host())

    async def disconnect(self) -> None:
        if self._soco is not None:
            try:
                await asyncio.to_thread(self._soco.stop)
            except Exception as e:  # noqa: BLE001 - teardown is best-effort
                logger.debug(f"[{self.renderer.name}] Sonos stop failed: {e}")

    async def load_queue(
        self, items: list[tuple[str, str, str]], start_index: int = 0
    ) -> None:
        soco_dev = self._soco

        def _load():
            soco_dev.clear_queue()
            for url, _title, _meta in items:
                soco_dev.add_uri_to_queue(url)
            soco_dev.play_from_queue(max(0, start_index))

        await asyncio.to_thread(_load)
        self._queue_len = len(items)
        self._current_index = max(0, min(start_index, self._queue_len - 1))

    async def go_next(self) -> bool:
        if self._current_index + 1 >= self._queue_len:
            return False
        await asyncio.to_thread(self._soco.next)
        self._current_index += 1
        return True

    async def go_previous(self) -> bool:
        if self._current_index <= 0:
            return False
        await asyncio.to_thread(self._soco.previous)
        self._current_index -= 1
        return True

    async def play(self) -> None:
        await asyncio.to_thread(self._soco.play)

    async def pause(self) -> None:
        await asyncio.to_thread(self._soco.pause)

    async def stop(self) -> None:
        await asyncio.to_thread(self._soco.stop)

    async def play_uri(self, url: str, title: str, metadata: str) -> None:
        raise RuntimeError("Sonos uses load_queue, not play_uri")

    async def preload_next(self, url: str, title: str, metadata: str) -> None:
        return  # the device manages its own queue/transitions

    async def query_transport_state(self) -> str | None:
        return await asyncio.to_thread(lambda: self.transport_state)

    async def refresh(self) -> None:
        return

    # -- volume / mute (soco exposes 0-100 volume + bool mute) -------------

    async def set_volume(self, volume: int) -> None:
        pct = max(0, min(100, volume))
        await asyncio.to_thread(setattr, self._soco, "volume", pct)

    async def get_volume(self) -> int | None:
        if self._soco is None:
            return None
        return int(await asyncio.to_thread(lambda: self._soco.volume))

    async def set_mute(self, mute: bool) -> None:
        await asyncio.to_thread(setattr, self._soco, "mute", bool(mute))

    async def get_mute(self) -> bool | None:
        if self._soco is None:
            return None
        return bool(await asyncio.to_thread(lambda: self._soco.mute))
