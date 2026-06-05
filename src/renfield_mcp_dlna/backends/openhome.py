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

PROVISIONAL — validated only against the OpenHome service spec + mocks, NOT a
real Linn yet (see tasks/todo.md T4/T10). It is therefore NOT auto-selected:
the factory routes OpenHome renderers here only when RENFIELD_OPENHOME=1, so an
unvalidated path can't regress Linn playback that currently works via the
AVTransport fallback. Enable it to run the spike against real hardware.
"""

import logging

from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.event_handler import UpnpEventHandler

from ..discovery import DlnaRenderer
from .base import PlaybackBackend, TransportEvent

logger = logging.getLogger(__name__)

_PLAYLIST_TYPE = "urn:av-openhome-org:service:Playlist:1"
_VOLUME_TYPE = "urn:av-openhome-org:service:Volume:1"


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

    def _service(self, service_type: str):
        if self._device is None:
            return None
        return self._device.services.get(service_type)

    def _playlist(self):
        svc = self._service(_PLAYLIST_TYPE)
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
        self._device = await factory.async_create_device(self.renderer.location)

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
        return self._transport_state

    async def refresh(self) -> None:
        return  # best-effort; OpenHome state tracking is command-driven here

    # -- volume / mute (OpenHome Volume service) ---------------------------

    def _volume_service(self):
        return self._service(_VOLUME_TYPE)

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
