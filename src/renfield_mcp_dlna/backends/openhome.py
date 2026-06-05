"""OpenHome backend (Linn and other ohNet renderers).

OpenHome renderers expose services under `urn:av-openhome-org` instead of (or
alongside) standard UPnP AVTransport/RenderingControl:

  * Playlist — the play queue lives ON THE DEVICE. You DeleteAll, Insert each
    track (URI + DIDL metadata) which returns a new track id, then Play /
    SeekIndex / Next / Previous. So owns_queue is True: QueueSession hands the
    whole queue over once (load_queue) and asks the device to advance.
  * Volume — raw integer volume + VolumeMax + Mute. This is the reliable path
    on Linn, whose RenderingControl advertises a bogus max (the workaround the
    AVTransport backend needs); the OpenHome Volume service reports a real max.

Validation status — VERIFIED end-to-end against real Linn (Sneaky DSM "Ben's
Zimmer", Majik DSM):
  * Sibling-device discovery (host-correlated openhome_location) + version-flexible
    service resolution (Volume:4).
  * volume/mute reads matched the AVTransport RenderingControl values.
  * PLAYBACK: load_queue (Insert→ids/SeekId/Play) reached real device
    TransportState=PLAYING; go_next advanced to track 2 (still PLAYING); stop
    cleaned up. Real state read via the Transport service (TransportState→State).
Still routed only when RENFIELD_OPENHOME=1 (a deliberate default — flipping all
Linn off AVTransport is the user's call), but it's no longer unproven.
"""

import logging

from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.event_handler import UpnpEventHandler

from ..discovery import DlnaRenderer
from .base import PlaybackBackend, TransportEvent

logger = logging.getLogger(__name__)

# Version-flexible prefixes — real Linn advertises Playlist:1, Volume:4, etc.
_PLAYLIST_PREFIX = "urn:av-openhome-org:service:Playlist:"
_VOLUME_PREFIX = "urn:av-openhome-org:service:Volume:"
_TRANSPORT_PREFIX = "urn:av-openhome-org:service:Transport:"

# OpenHome Transport.TransportState values → the standard UPnP vocabulary the
# rest of the code (TRANSPORT_OK/DEAD, status) speaks.
_OH_STATE_MAP = {
    "PLAYING": "PLAYING",
    "PAUSED": "PAUSED_PLAYBACK",
    "STOPPED": "STOPPED",
    "BUFFERING": "TRANSITIONING",
}


class OpenHomeBackend(PlaybackBackend):
    """Playback via the OpenHome Playlist + Volume services."""

    owns_queue = True

    def __init__(self, renderer: DlnaRenderer):
        self.renderer = renderer
        self._device = None
        self._on_event: TransportEvent | None = None
        self._transport_state: str | None = None
        # Device-assigned track ids, in queue order, captured at load_queue time
        # so go_next/go_previous and status can map index <-> id.
        self._track_ids: list[int] = []
        self._current_index = 0

    # -- capability / state ------------------------------------------------

    @property
    def supports_next(self) -> bool:
        return True  # the device manages gapless transitions natively

    @property
    def connected(self) -> bool:
        return self._device is not None

    @property
    def transport_state(self) -> str | None:
        return self._transport_state

    # -- services ----------------------------------------------------------

    def _service(self, type_prefix: str):
        """Find a service whose type starts with `type_prefix` (version-flexible)."""
        if self._device is None:
            return None
        for service_type, svc in self._device.services.items():
            if service_type.startswith(type_prefix):
                return svc
        return None

    def _playlist(self):
        svc = self._service(_PLAYLIST_PREFIX)
        if svc is None:
            raise RuntimeError("OpenHome renderer has no Playlist service")
        return svc

    # -- connection lifecycle ----------------------------------------------

    async def connect(
        self,
        on_event: TransportEvent,
        *,
        factory: UpnpFactory,
        event_handler: UpnpEventHandler,
    ) -> None:
        self._on_event = on_event
        # OpenHome services live on the sibling device (Linn), not the renderer's
        # MediaRenderer description — use the correlated openhome_location.
        location = self.renderer.openhome_location or self.renderer.location
        self._device = await factory.async_create_device(location)

    async def disconnect(self) -> None:
        if self._device is None:
            return
        try:
            await self._playlist().action("Stop").async_call()
        except Exception as e:  # noqa: BLE001 - teardown is best-effort
            logger.debug(f"[{self.renderer.name}] OpenHome stop failed: {e}")

    # -- device-owned queue ------------------------------------------------

    async def load_queue(
        self, items: list[tuple[str, str, str]], start_index: int = 0
    ) -> None:
        """DeleteAll, Insert each track (capturing its id), then play start_index."""
        pl = self._playlist()
        await pl.action("DeleteAll").async_call()
        self._track_ids = []
        after_id = 0  # 0 = insert at head
        for url, _title, meta in items:
            res = await pl.action("Insert").async_call(
                AfterId=after_id, Uri=url, Metadata=meta
            )
            new_id = int(res["NewId"])
            self._track_ids.append(new_id)
            after_id = new_id

        if not self._track_ids:
            return
        start_index = max(0, min(start_index, len(self._track_ids) - 1))
        self._current_index = start_index
        await pl.action("SeekId").async_call(Value=self._track_ids[start_index])
        await pl.action("Play").async_call()
        self._transport_state = "PLAYING"

    async def go_next(self) -> bool:
        if self._current_index + 1 >= len(self._track_ids):
            return False
        await self._playlist().action("Next").async_call()
        self._current_index += 1
        return True

    async def go_previous(self) -> bool:
        if self._current_index <= 0:
            return False
        await self._playlist().action("Previous").async_call()
        self._current_index -= 1
        return True

    # -- transport commands ------------------------------------------------

    async def play(self) -> None:
        await self._playlist().action("Play").async_call()
        self._transport_state = "PLAYING"

    async def pause(self) -> None:
        await self._playlist().action("Pause").async_call()
        self._transport_state = "PAUSED_PLAYBACK"

    async def stop(self) -> None:
        await self._playlist().action("Stop").async_call()
        self._transport_state = "STOPPED"

    async def seek(self, position_seconds: int) -> None:
        await self._playlist().action("SeekSecondAbsolute").async_call(
            Value=max(0, position_seconds)
        )

    # play_uri/preload_next are part of the client-owned-queue contract; an
    # OpenHome session goes through load_queue instead, so these are no-ops that
    # raise if ever called via the wrong path.
    async def play_uri(self, url: str, title: str, metadata: str) -> None:
        raise RuntimeError("OpenHome uses load_queue, not play_uri")

    async def preload_next(self, url: str, title: str, metadata: str) -> None:
        return  # device manages gapless natively

    async def query_transport_state(self) -> str | None:
        """Read the REAL device state from the OpenHome Transport service
        (action TransportState → out arg State), mapped to standard vocabulary.
        Falls back to the optimistic cache if the Transport service is absent."""
        svc = self._service(_TRANSPORT_PREFIX)
        if svc is None:
            return self._transport_state
        try:
            res = await svc.action("TransportState").async_call()
            raw = str(res.get("State", "")).upper()
        except Exception:  # noqa: BLE001 - best-effort read
            return self._transport_state
        mapped = _OH_STATE_MAP.get(raw)
        if mapped:
            self._transport_state = mapped
        return self._transport_state

    async def refresh(self) -> None:
        await self.query_transport_state()

    # -- volume / mute (OpenHome Volume service) ---------------------------

    def _volume_service(self):
        return self._service(_VOLUME_PREFIX)

    async def _volume_max(self, vol) -> int:
        try:
            res = await vol.action("Characteristics").async_call()
            mx = int(res.get("VolumeMax", 100))
            return mx if 2 <= mx <= 1000 else 100
        except Exception:  # noqa: BLE001 - missing/odd → safe default
            return 100

    async def set_volume(self, volume: int) -> None:
        vol = self._volume_service()
        if vol is None:
            raise RuntimeError("OpenHome renderer has no Volume service")
        pct = max(0, min(100, volume))
        scale = await self._volume_max(vol)
        await vol.action("SetVolume").async_call(
            Value=max(0, min(scale, round(pct / 100 * scale)))
        )

    async def get_volume(self) -> int | None:
        vol = self._volume_service()
        if vol is None:
            return None
        try:
            res = await vol.action("Volume").async_call()
            scale = await self._volume_max(vol)
            return max(0, min(100, round(int(res["Value"]) / scale * 100)))
        except Exception:  # noqa: BLE001 - best-effort read
            return None

    async def set_mute(self, mute: bool) -> None:
        vol = self._volume_service()
        if vol is None:
            raise RuntimeError("OpenHome renderer has no Volume service")
        await vol.action("SetMute").async_call(Value=mute)

    async def get_mute(self) -> bool | None:
        vol = self._volume_service()
        if vol is None:
            return None
        try:
            res = await vol.action("Mute").async_call()
            return bool(res["Value"])
        except Exception:  # noqa: BLE001 - best-effort read
            return None
