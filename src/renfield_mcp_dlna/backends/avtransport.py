"""Standard UPnP AVTransport backend (the default for most DLNA renderers).

Wraps async_upnp_client's DmrDevice for transport commands and talks to the
RenderingControl service DIRECTLY for volume/mute. The queue is owned
client-side by the QueueSession; this backend just plays whatever URI it is
handed and preloads the next one via SetNextAVTransportURI.

Event flow:

    device LAST_CHANGE ──▶ _handle_raw_event(service, state_vars)
                              │  parse list[UpnpStateVariable] → {name: value}
                              │  cache Volume (normalised to %) and TransportState
                              └─▶ on_event(transport_state, current_uri)  ──▶ QueueSession
"""

import asyncio
import logging
from datetime import timedelta

from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.event_handler import UpnpEventHandler
from async_upnp_client.profiles.dlna import DmrDevice, PlayMode

from ..discovery import DlnaRenderer
from .base import PlaybackBackend, TransportEvent

logger = logging.getLogger(__name__)

_RENDERING_CONTROL_TYPE = "urn:schemas-upnp-org:service:RenderingControl:1"

# Hard wall-clock budget for a single GetTransportInfo / GetVolume poll.
# async_update() issues several sequential SOAP calls each with the library's
# default 5s timeout — without a budget a hung renderer could block get_status
# for tens of seconds.
_TRANSPORT_POLL_TIMEOUT = 3.0


class AvTransportBackend(PlaybackBackend):
    """Playback via UPnP AVTransport + RenderingControl on a single renderer."""

    owns_queue = False

    def __init__(self, renderer: DlnaRenderer):
        self.renderer = renderer
        self._dmr: DmrDevice | None = None
        self._on_event: TransportEvent | None = None
        # Last TransportState reported by the renderer (event or active poll).
        # Source of truth for status — never assume "playing" just because the
        # backend is bound.
        self._transport_state: str | None = None
        # Last RenderingControl Volume (0-100) seen via events or written by
        # set_volume — cached so get_volume avoids a SOAP round-trip.
        self._volume: int | None = None

    # -- capability / state ------------------------------------------------

    @property
    def supports_next(self) -> bool:
        return self.renderer.supports_next

    @property
    def connected(self) -> bool:
        return self._dmr is not None

    @property
    def transport_state(self) -> str | None:
        return self._transport_state

    # -- connection lifecycle ----------------------------------------------

    async def connect(
        self,
        on_event: TransportEvent,
        *,
        factory: UpnpFactory,
        event_handler: UpnpEventHandler,
    ) -> None:
        """Create the DmrDevice, wire the raw event handler, and subscribe.

        `factory`/`event_handler` are injected (owned by the shared UPnP
        infrastructure) so the backend stays infra-agnostic.
        """
        self._on_event = on_event
        device = await factory.async_create_device(self.renderer.location)
        self._dmr = DmrDevice(device, event_handler=event_handler)
        self._dmr.on_event = self._handle_raw_event
        await self._dmr.async_subscribe_services(auto_resubscribe=True)

    async def disconnect(self) -> None:
        if self._dmr is None:
            return
        try:
            await self._dmr.async_stop()
        except Exception as e:  # noqa: BLE001 - teardown is best-effort
            logger.debug(f"[{self.renderer.name}] Stop failed: {e}")
        try:
            await self._dmr.async_unsubscribe_services()
        except Exception as e:  # noqa: BLE001 - teardown is best-effort
            logger.debug(f"[{self.renderer.name}] Unsubscribe failed: {e}")

    # -- transport commands ------------------------------------------------

    async def play_uri(self, url: str, title: str, metadata: str) -> None:
        assert self._dmr is not None
        await self._dmr.async_set_transport_uri(url, title, metadata)
        await self._dmr.async_play()

    async def preload_next(self, url: str, title: str, metadata: str) -> None:
        assert self._dmr is not None
        await self._dmr.async_set_next_transport_uri(url, title, metadata)

    async def play(self) -> None:
        if self._dmr is None:
            raise RuntimeError("No active playback session")
        await self._dmr.async_play()

    async def pause(self) -> None:
        if self._dmr is None:
            raise RuntimeError("No active playback session")
        await self._dmr.async_pause()

    async def stop(self) -> None:
        if self._dmr is not None:
            await self._dmr.async_stop()

    # -- transport state polling -------------------------------------------

    async def query_transport_state(self) -> str | None:
        """Actively poll the renderer for its current TransportState.

        Fallback for renderers that don't emit LAST_CHANGE events: calls
        GetTransportInfo via async_update() and returns the raw UPnP state
        string (e.g. "PLAYING"), or None if the poll fails / yields nothing.
        """
        if self._dmr is None:
            return None
        try:
            await asyncio.wait_for(
                self._dmr.async_update(), timeout=_TRANSPORT_POLL_TIMEOUT
            )
        except Exception as e:  # noqa: BLE001 - refresh is best-effort
            # Fall through: the lib keeps transport_state fresh from the event
            # subscription, so a failed/timed-out active refresh shouldn't blank
            # an otherwise-known state.
            logger.debug(f"[{self.renderer.name}] transport poll refresh failed: {e}")
        ts = self._dmr.transport_state
        if ts is None:
            return None
        state = str(getattr(ts, "value", ts) or "").upper()
        # The lib returns VENDOR_DEFINED when the state var exists but was never
        # populated — that's "unknown", not a real transport state.
        if not state or state == "VENDOR_DEFINED":
            return None
        return state

    async def query_playback(self) -> tuple[str | None, int | None]:
        """One GetTransportInfo+GetPositionInfo refresh; returns (state, pos_s).

        A single ``async_update`` round-trip so the playback-confirm loop can
        read both the polled TransportState and the position without two device
        calls (which wouldn't fit the confirm window). ``media_position`` is in
        seconds (lib may give an int or a timedelta)."""
        if self._dmr is None:
            return None, None
        try:
            await asyncio.wait_for(
                self._dmr.async_update(), timeout=_TRANSPORT_POLL_TIMEOUT
            )
        except Exception as e:  # noqa: BLE001 - best-effort, like query_transport_state
            logger.debug(f"[{self.renderer.name}] playback poll refresh failed: {e}")
        ts = self._dmr.transport_state
        state = str(getattr(ts, "value", ts) or "").upper() if ts is not None else ""
        if not state or state == "VENDOR_DEFINED":
            state = None
        pos = self._dmr.media_position
        if pos is None:
            pos_s: int | None = None
        elif isinstance(pos, timedelta):
            pos_s = int(pos.total_seconds())
        else:
            pos_s = int(pos)
        return state, pos_s

    async def refresh(self) -> None:
        """Best-effort active poll that updates the cached transport_state.

        Used by get_status so status() reports the renderer's true state even
        on event-silent renderers. Leaves the last known state untouched if the
        poll fails.
        """
        polled = await self.query_transport_state()
        if polled:
            self._transport_state = polled

    # -- raw event handling ------------------------------------------------

    def _handle_raw_event(self, service, state_variables) -> None:
        """Parse a LAST_CHANGE event, cache volume + transport state, and
        forward (transport_state, current_uri) to the session.

        async_upnp_client delivers a **list** of changed UpnpStateVariable
        objects (each with .name/.value), NOT a dict — calling .get() on it
        raised AttributeError, which meant TransportState was never captured
        (so status reported "unknown") and gapless/auto-advance detection
        silently never fired. Fold the list into a name->value map; a dict is
        tolerated defensively for forward/backward compat.
        """
        if not state_variables:
            return

        if isinstance(state_variables, dict):
            changes = state_variables
        else:
            changes = {
                getattr(sv, "name", None): getattr(sv, "value", None)
                for sv in state_variables
            }

        self._cache_volume_from_event(changes.get("Volume"))

        transport_state = changes.get("TransportState")
        current_uri = changes.get("CurrentTrackURI")
        if transport_state:
            self._transport_state = transport_state

        logger.debug(
            f"[{self.renderer.name}] Event: state={transport_state}, uri={current_uri}"
        )
        if self._on_event is not None:
            self._on_event(transport_state, current_uri)

    def _cache_volume_from_event(self, volume) -> None:
        """Cache a RenderingControl Volume event value, normalised to percent.

        The event value is in the renderer's native units, so normalise with
        the same scale get_volume/set_volume use — otherwise a 0-255 renderer
        would cache 128 and report it as "128%".
        """
        if volume is None:
            return
        try:
            raw = int(volume)
        except (TypeError, ValueError):
            return
        rc = self._rendering_control()
        if rc is not None:
            scale = self._volume_scale(rc)
            self._volume = max(0, min(100, round(raw / scale * 100)))
        elif 0 <= raw <= 100:
            # RC not resolvable yet; only trust an already-percent value.
            self._volume = raw

    # -- volume / mute (direct RenderingControl) ---------------------------
    #
    # Volume/mute go through the RenderingControl service DIRECTLY (raw 0-100
    # SetVolume/GetVolume/SetMute/GetMute), NOT async_upnp_client's DmrDevice
    # helpers. DmrDevice normalises by the advertised volume range, and some
    # renderers (Linn) advertise a bogus max (2^31-1) — async_set_volume_level
    # would then send ~858M for "40%" and the renderer clamps to its real max
    # = full-blast. DmrDevice.volume_level / has_volume_mute also read as
    # None/False on those renderers even though the RC actions work fine.
    # (Once OpenHomeBackend lands, Linn uses its OpenHome Volume service and
    # this workaround is only the generic-AVTransport fallback.)

    def _rendering_control(self):
        """The RenderingControl service for direct action calls, or None."""
        if self._dmr is None:
            return None
        return self._dmr.device.services.get(_RENDERING_CONTROL_TYPE)

    @staticmethod
    def _volume_scale(rc) -> int:
        """Renderer's RC volume max. Honours a sane advertised range, else 100.

        Linn advertises 0..2147483647 (no real range) — treated as 0-100, the
        scale its CurrentVolume actually uses.
        """
        try:
            mx = rc.action("SetVolume").argument("DesiredVolume").related_state_variable.max_value
            if mx and 2 <= int(mx) <= 1000:
                return int(mx)
        except Exception:  # noqa: BLE001 - missing/odd range -> safe default
            pass
        return 100

    async def set_volume(self, volume: int) -> None:
        if self._dmr is None:
            raise RuntimeError("No active playback session")
        rc = self._rendering_control()
        if rc is None or "SetVolume" not in rc.actions:
            raise RuntimeError("Renderer does not support volume control")
        pct = max(0, min(100, volume))
        scale = self._volume_scale(rc)
        desired = max(0, min(scale, round(pct / 100 * scale)))
        await rc.action("SetVolume").async_call(
            InstanceID=0, Channel="Master", DesiredVolume=desired
        )
        # Reflect our own write in the cache (in percent) so get_volume stays
        # consistent without waiting for the renderer's echoed Volume event.
        self._volume = pct

    async def set_mute(self, mute: bool) -> None:
        """Mute (True) or unmute (False) via direct RC SetMute.

        Mute is tracked independently of the volume level, so unmute restores
        the prior volume without us storing it. Capability is detected by the
        SetMute action being present (NOT DmrDevice.has_volume_mute, which reads
        False on renderers whose GetMute the DmrDevice abstraction can't parse).
        """
        if self._dmr is None:
            raise RuntimeError("No active playback session")
        rc = self._rendering_control()
        if rc is None or "SetMute" not in rc.actions:
            raise RuntimeError("Renderer does not support mute")
        await rc.action("SetMute").async_call(
            InstanceID=0, Channel="Master", DesiredMute=mute
        )

    async def get_mute(self) -> bool | None:
        """Current mute state via direct RC GetMute, or None if unreportable."""
        rc = self._rendering_control()
        if rc is None or "GetMute" not in rc.actions:
            return None
        try:
            res = await asyncio.wait_for(
                rc.action("GetMute").async_call(InstanceID=0, Channel="Master"),
                timeout=_TRANSPORT_POLL_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 - read is best-effort
            return None
        cur = res.get("CurrentMute")
        return bool(cur) if cur is not None else None

    # -- position / duration / capabilities (from DmrDevice) ---------------
    # These read whatever the last async_update() populated; get_status calls
    # refresh() first so they reflect a fresh GetPositionInfo/GetTransportInfo.

    @property
    def media_position(self) -> int | None:
        return getattr(self._dmr, "media_position", None) if self._dmr else None

    @property
    def media_duration(self) -> int | None:
        return getattr(self._dmr, "media_duration", None) if self._dmr else None

    @property
    def capabilities(self) -> dict:
        """can_pause/seek/next/previous from the device's current transport
        actions (DmrDevice exposes these as properties)."""
        d = self._dmr
        if d is None:
            return {}
        return {
            "can_pause": bool(getattr(d, "can_pause", False)),
            "can_seek": bool(
                getattr(d, "can_seek_rel_time", False)
                or getattr(d, "can_seek_abs_time", False)
            ),
            "can_next": bool(getattr(d, "can_next", False)),
            "can_previous": bool(getattr(d, "can_previous", False)),
        }

    @property
    def valid_play_modes(self) -> set[str]:
        if self._dmr is None:
            return set()
        try:
            return {str(getattr(m, "value", m)).lower() for m in self._dmr.valid_play_modes}
        except Exception:  # noqa: BLE001 - absent/odd state var → none
            return set()

    async def seek(self, position_seconds: int) -> None:
        """Seek to an absolute offset within the current track (UPnP REL_TIME).

        REL_TIME is, confusingly, the position relative to the *start of the
        track* — i.e. where most UIs mean by "seek to 1:30".
        """
        if self._dmr is None:
            raise RuntimeError("No active playback session")
        if not getattr(self._dmr, "can_seek_rel_time", False):
            raise RuntimeError("Renderer does not support seek")
        await self._dmr.async_seek_rel_time(timedelta(seconds=max(0, position_seconds)))

    async def set_play_mode(self, mode: str) -> None:
        if self._dmr is None:
            raise RuntimeError("No active playback session")
        normalized = mode.strip().lower()
        if normalized not in self.valid_play_modes:
            raise RuntimeError(
                f"Renderer does not support play mode '{mode}' "
                f"(supports: {sorted(self.valid_play_modes) or 'none'})"
            )
        await self._dmr.async_set_play_mode(PlayMode(normalized.upper()))

    async def get_volume(self) -> int | None:
        """Current volume (0-100), or None if the renderer can't report it.

        Prefers the cached value maintained by _handle_raw_event / set_volume
        (no SOAP round-trip). Falls back to a bounded direct RC GetVolume read.
        """
        if self._volume is not None:
            return self._volume
        rc = self._rendering_control()
        if rc is None or "GetVolume" not in rc.actions:
            return None
        try:
            res = await asyncio.wait_for(
                rc.action("GetVolume").async_call(InstanceID=0, Channel="Master"),
                timeout=_TRANSPORT_POLL_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 - read is best-effort
            return None
        cur = res.get("CurrentVolume")
        if cur is None:
            return None
        scale = self._volume_scale(rc)
        vol = max(0, min(100, round(int(cur) / scale * 100)))
        # A Volume event may have landed via _handle_raw_event while the read
        # ran; that value is fresher, so don't clobber it.
        if self._volume is None:
            self._volume = vol
        return vol
