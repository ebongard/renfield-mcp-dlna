"""Queue state machine for DLNA renderer playback with UPnP event subscription."""

import asyncio
import logging
import os
import socket
import time
from dataclasses import dataclass, field

from async_upnp_client.aiohttp import AiohttpNotifyServer, AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.event_handler import UpnpEventHandler
from async_upnp_client.profiles.dlna import DmrDevice

from .didl import build_didl_metadata
from .discovery import DlnaRenderer

logger = logging.getLogger(__name__)

# Module-level shared UPnP event infrastructure
_requester: AiohttpRequester | None = None
_notify_server: AiohttpNotifyServer | None = None
_event_handler: UpnpEventHandler | None = None
_factory: UpnpFactory | None = None

# Session registry: renderer UDN → QueueSession
_sessions: dict[str, "QueueSession"] = {}

# UPnP AVTransport TransportState values (raw strings from LAST_CHANGE events).
# A renderer that actually started playback reaches PLAYING; one that couldn't
# fetch/decode the stream stays STOPPED / NO_MEDIA_PRESENT.
_TRANSPORT_OK = {"PLAYING", "PAUSED_PLAYBACK"}
_TRANSPORT_DEAD = {"STOPPED", "NO_MEDIA_PRESENT"}
# How long start() waits for the renderer to confirm it began playing.
_PLAYBACK_CONFIRM_TIMEOUT = 5.0
_PLAYBACK_CONFIRM_INTERVAL = 0.5
# Hard wall-clock budget for a single GetTransportInfo poll. async_update()
# issues several sequential SOAP calls (and a larger burst on the first poll),
# each with the library's default 5s timeout — without a budget a hung renderer
# could block get_status for tens of seconds.
_TRANSPORT_POLL_TIMEOUT = 3.0


def _detect_local_ip() -> str:
    """Detect local IP that can reach the network (for UPnP callback URL)."""
    env_ip = os.environ.get("DLNA_LISTEN_IP")
    if env_ip:
        return env_ip
    # Connect to a public IP to determine our local interface
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("239.255.255.250", 1900))
        return s.getsockname()[0]
    except Exception:
        return "0.0.0.0"
    finally:
        s.close()


async def _ensure_infrastructure() -> None:
    """Start shared notify server + event handler (idempotent)."""
    global _requester, _notify_server, _event_handler, _factory
    if _notify_server is not None:
        return

    source_ip = _detect_local_ip()
    _requester = AiohttpRequester()
    _notify_server = AiohttpNotifyServer(
        requester=_requester,
        source=(source_ip, 0),  # OS picks free port
    )
    _event_handler = UpnpEventHandler(_notify_server, _requester)
    _factory = UpnpFactory(_requester)
    await _notify_server.async_start_server()
    logger.info(
        f"UPnP notify server started on {source_ip}, "
        f"callback: {_notify_server.callback_url}"
    )


async def _shutdown_infrastructure() -> None:
    """Stop notify server when no sessions remain."""
    global _notify_server, _requester, _event_handler, _factory
    if _notify_server:
        await _notify_server.async_stop_server()
        logger.info("UPnP notify server stopped")
    _notify_server = _requester = _event_handler = _factory = None


@dataclass
class Track:
    """A single track in the playback queue."""

    url: str
    title: str = ""
    artist: str = ""
    album: str = ""
    art_url: str = ""
    media_type: str = "audio"  # "audio" or "video"


class QueueSession:
    """Manages queue playback on a single DLNA renderer.

    Uses UPnP event subscription (LAST_CHANGE) for track transition
    detection — no polling.
    """

    def __init__(self, renderer: DlnaRenderer, tracks: list[Track]):
        self.renderer = renderer
        self.tracks = tracks
        self.current_index = 0
        self._dmr: DmrDevice | None = None
        self._preloaded_index: int | None = None
        # Last TransportState reported by the renderer via LAST_CHANGE events.
        # Source of truth for status() — never assume "playing" just because a
        # renderer is bound.
        self._transport_state: str | None = None
        # Last RenderingControl Volume (0-100) seen via LAST_CHANGE events or
        # written by set_volume — cached so get_volume avoids a SOAP round-trip.
        self._volume: int | None = None
        # Guards the no-SetNext auto-advance against duplicate STOPPED events
        # firing a second _auto_advance before the first incremented the index.
        self._advancing = False

    def _build_metadata(self, track: Track) -> str:
        """Build DIDL-Lite metadata based on track media type."""
        if track.media_type == "video":
            from .didl import build_video_didl_metadata
            return build_video_didl_metadata(track.url, track.title)
        return build_didl_metadata(track.url, track.title, track.artist, track.album, track.art_url)

    async def start(self) -> None:
        """Subscribe to events, play track 1, preload track 2."""
        await _ensure_infrastructure()
        assert _factory is not None
        assert _event_handler is not None

        device = await _factory.async_create_device(self.renderer.location)
        self._dmr = DmrDevice(device, event_handler=_event_handler)
        self._dmr.on_event = self._on_event

        # Subscribe to AVTransport events
        await self._dmr.async_subscribe_services(auto_resubscribe=True)

        # Play first track
        track = self.tracks[0]
        metadata = self._build_metadata(track)
        await self._dmr.async_set_transport_uri(track.url, track.title, metadata)
        await self._dmr.async_play()

        # Verify the renderer actually started — a 404/unreachable stream leaves
        # it STOPPED/NO_MEDIA_PRESENT. Surface that as a failure instead of
        # logging "Playing" and letting status() falsely report success.
        await self._confirm_playback_started(track.title)

        logger.info(f"[{self.renderer.name}] Playing track 1/{len(self.tracks)}: {track.title}")

        # Preload second track if renderer supports it
        if len(self.tracks) > 1 and self.renderer.supports_next:
            await self._preload_next()

    async def _confirm_playback_started(self, title: str) -> None:
        """Wait briefly for the renderer to confirm it began playback.

        Raises RuntimeError if the renderer reports STOPPED/NO_MEDIA_PRESENT
        (it accepted the command but couldn't play the stream — e.g. a 404
        media URL). If no definitive state arrives within the timeout, log a
        warning and continue rather than false-failing a renderer that simply
        hasn't emitted an event yet.
        """
        # Wall-clock deadline: the poll fallback below can itself take seconds,
        # so summing the sleep interval would badly undercount elapsed time.
        deadline = time.monotonic() + _PLAYBACK_CONFIRM_TIMEOUT
        while time.monotonic() < deadline:
            state = (self._transport_state or "").upper()
            # Renderers that don't emit LAST_CHANGE events (e.g. HiFiBerryOS)
            # never populate _transport_state — actively poll GetTransportInfo
            # so we can still confirm (or rule out) playback.
            if not state:
                state = (await self._query_transport_state()) or ""
            if state in _TRANSPORT_OK:
                return
            if state in _TRANSPORT_DEAD:
                raise RuntimeError(
                    f"renderer did not start playback (state={state}); "
                    f"stream may be unreachable: {title}"
                )
            await asyncio.sleep(_PLAYBACK_CONFIRM_INTERVAL)
        logger.warning(
            f"[{self.renderer.name}] playback start unconfirmed for '{title}' "
            f"(last state={self._transport_state})"
        )

    async def _query_transport_state(self) -> str | None:
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

    async def refresh_state(self) -> None:
        """Best-effort refresh of the cached TransportState via an active poll.

        Called by get_status so status() reports the renderer's true state even
        on event-silent renderers. Leaves the last known state untouched if the
        poll fails."""
        polled = await self._query_transport_state()
        if polled:
            self._transport_state = polled

    def _on_event(self, service, state_variables) -> None:
        """Handle AVTransport LAST_CHANGE events.

        async_upnp_client delivers a **list** of changed UpnpStateVariable
        objects (each with .name/.value), NOT a dict — calling .get() on it
        raised AttributeError, which meant TransportState was never captured
        (so status reported "unknown") and gapless-transition / auto-advance
        detection silently never fired. Fold the list into a name->value map;
        a dict is tolerated defensively for forward/backward compat.
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

        transport_state = changes.get("TransportState")
        current_uri = changes.get("CurrentTrackURI")

        # RenderingControl Volume rides on the same event stream; cache it so
        # get_volume can answer without a SOAP round-trip.
        volume = changes.get("Volume")
        if volume is not None:
            try:
                self._volume = int(volume)
            except (TypeError, ValueError):
                pass

        # Did the renderer actually reach a playing state for this track? A
        # transient STOPPED/NO_MEDIA_PRESENT during the initial buffering window
        # must NOT be mistaken for track-end (it would skip track 1 or tear down
        # a single-track session before it ever played).
        played = (self._transport_state or "").upper() in _TRANSPORT_OK

        if transport_state:
            self._transport_state = transport_state

        logger.debug(
            f"[{self.renderer.name}] Event: state={transport_state}, uri={current_uri}"
        )

        # Detect gapless transition: renderer switched to preloaded track
        if current_uri and self._preloaded_index is not None:
            preloaded_track = self.tracks[self._preloaded_index]
            if current_uri == preloaded_track.url:
                self.current_index = self._preloaded_index
                self._preloaded_index = None
                logger.info(
                    f"[{self.renderer.name}] Transitioned to track "
                    f"{self.current_index + 1}/{len(self.tracks)}: "
                    f"{preloaded_track.title}"
                )
                asyncio.create_task(self._preload_next())
                return

        # Detect track end for renderers WITHOUT SetNext support:
        # When transport stops AFTER having played, and we have more tracks,
        # auto-advance. _advancing dedupes duplicate STOPPED events that would
        # otherwise fire a second advance and skip a track.
        if (
            transport_state == "STOPPED"
            and played
            and not self.renderer.supports_next
            and self.current_index < len(self.tracks) - 1
            and not self._advancing
        ):
            logger.info(
                f"[{self.renderer.name}] Track ended (no gapless), advancing..."
            )
            self._advancing = True
            asyncio.create_task(self._auto_advance())
            return

        # Album finished — only after the last track actually played (guards
        # against a transient pre-playback STOPPED tearing down the session).
        if (
            transport_state == "STOPPED"
            and played
            and self.current_index >= len(self.tracks) - 1
        ):
            logger.info(f"[{self.renderer.name}] Queue finished")
            asyncio.create_task(self._cleanup())

    async def _auto_advance(self) -> None:
        """Advance to next track on renderers without SetNext (small gap)."""
        try:
            if self.current_index + 1 >= len(self.tracks):
                return
            self.current_index += 1
            self._preloaded_index = None
            track = self.tracks[self.current_index]
            metadata = self._build_metadata(track)
            try:
                assert self._dmr is not None
                await self._dmr.async_set_transport_uri(track.url, track.title, metadata)
                await self._dmr.async_play()
                logger.info(
                    f"[{self.renderer.name}] Auto-advanced to track "
                    f"{self.current_index + 1}/{len(self.tracks)}: {track.title}"
                )
            except Exception as e:
                logger.error(f"[{self.renderer.name}] Auto-advance failed: {e}")
        finally:
            # Re-arm for the next track's STOPPED. The `played` gate prevents a
            # premature re-advance until the new track actually reaches PLAYING.
            self._advancing = False

    async def _preload_next(self) -> None:
        """Preload the next track via SetNextAVTransportURI."""
        next_idx = self.current_index + 1
        if next_idx >= len(self.tracks) or not self.renderer.supports_next:
            return
        if self._dmr is None:
            return

        track = self.tracks[next_idx]
        metadata = self._build_metadata(track)
        try:
            await self._dmr.async_set_next_transport_uri(
                track.url, track.title, metadata
            )
            self._preloaded_index = next_idx
            logger.debug(
                f"[{self.renderer.name}] Preloaded track {next_idx + 1}: {track.title}"
            )
        except Exception as e:
            logger.warning(f"[{self.renderer.name}] Preload failed: {e}")
            self._preloaded_index = None

    async def next(self) -> Track | None:
        """Skip to next track immediately."""
        if self.current_index + 1 >= len(self.tracks):
            return None
        self.current_index += 1
        self._preloaded_index = None
        track = self.tracks[self.current_index]
        metadata = self._build_metadata(track)
        assert self._dmr is not None
        await self._dmr.async_set_transport_uri(track.url, track.title, metadata)
        await self._dmr.async_play()
        await self._preload_next()
        logger.info(
            f"[{self.renderer.name}] Skipped to track "
            f"{self.current_index + 1}/{len(self.tracks)}: {track.title}"
        )
        return track

    async def previous(self) -> Track | None:
        """Go to previous track."""
        if self.current_index <= 0:
            return None
        self.current_index -= 1
        self._preloaded_index = None
        track = self.tracks[self.current_index]
        metadata = self._build_metadata(track)
        assert self._dmr is not None
        await self._dmr.async_set_transport_uri(track.url, track.title, metadata)
        await self._dmr.async_play()
        await self._preload_next()
        logger.info(
            f"[{self.renderer.name}] Back to track "
            f"{self.current_index + 1}/{len(self.tracks)}: {track.title}"
        )
        return track

    async def stop(self) -> None:
        """Stop playback, unsubscribe, cleanup."""
        if self._dmr:
            try:
                await self._dmr.async_stop()
            except Exception as e:
                logger.debug(f"[{self.renderer.name}] Stop failed: {e}")
            try:
                await self._dmr.async_unsubscribe_services()
            except Exception as e:
                logger.debug(f"[{self.renderer.name}] Unsubscribe failed: {e}")
        logger.info(f"[{self.renderer.name}] Stopped and cleaned up")
        await self._cleanup()

    async def pause(self) -> None:
        """Pause playback."""
        if self._dmr is None:
            raise RuntimeError("No active playback session")
        await self._dmr.async_pause()

    async def resume(self) -> None:
        """Resume playback."""
        if self._dmr is None:
            raise RuntimeError("No active playback session")
        await self._dmr.async_play()

    async def set_volume(self, volume: int) -> None:
        """Set playback volume (0-100)."""
        if self._dmr is None:
            raise RuntimeError("No active playback session")
        await self._dmr.async_set_volume_level(volume / 100.0)
        # Reflect our own write in the cache so get_volume stays consistent
        # without waiting for the renderer's echoed Volume event.
        self._volume = volume

    async def get_volume(self) -> int | None:
        """Current volume (0-100), or None if the renderer can't report it.

        Prefers the cached value maintained by _on_event / set_volume (no SOAP
        round-trip). Falls back to a bounded async_update() read, mirroring
        refresh_state()'s timeout handling.
        """
        if self._volume is not None:
            return self._volume
        if self._dmr is None:
            return None
        try:
            await asyncio.wait_for(
                self._dmr.async_update(), timeout=_TRANSPORT_POLL_TIMEOUT
            )
        except Exception:  # noqa: BLE001 - read is best-effort, mirror _query_transport_state
            return None
        level = self._dmr.volume_level
        if level is None:
            return None
        vol = round(level * 100)
        # A Volume event may have landed via _on_event while async_update() ran;
        # that value is fresher than this poll, so don't clobber it.
        if self._volume is None:
            self._volume = vol
        return vol

    async def _cleanup(self) -> None:
        """Remove session from registry, shutdown infra if last."""
        udn = self.renderer.udn
        if udn in _sessions:
            del _sessions[udn]
        if not _sessions:
            await _shutdown_infrastructure()

    def _playback_state(self) -> str:
        """Map the renderer's last reported TransportState to a status string.

        Reports what the renderer actually says — never "playing" merely
        because a renderer is bound (the old behavior, which lied when the
        stream failed).
        """
        if self._dmr is None:
            return "stopped"
        state = (self._transport_state or "").upper()
        if state in _TRANSPORT_OK:
            return "paused" if state == "PAUSED_PLAYBACK" else "playing"
        if state in _TRANSPORT_DEAD:
            return "stopped"
        # Transitioning / not yet reported — say so, don't claim "playing".
        return state.lower() if state else "unknown"

    def status(self) -> dict:
        """Return current playback status."""
        track = self.tracks[self.current_index] if self.tracks else None
        return {
            "renderer": self.renderer.name,
            "state": self._playback_state(),
            "track": self.current_index + 1,
            "total_tracks": len(self.tracks),
            "title": track.title if track else None,
            "artist": track.artist if track else None,
            "album": track.album if track else None,
        }


async def play_tracks(renderer: DlnaRenderer, tracks: list[Track]) -> QueueSession:
    """Create and start a new queue session, replacing any existing one."""
    # Stop existing session on this renderer
    existing = _sessions.get(renderer.udn)
    if existing:
        await existing.stop()

    session = QueueSession(renderer, tracks)
    _sessions[renderer.udn] = session
    try:
        await session.start()
    except Exception:
        # start() failed (e.g. the renderer never began playback) — don't leave
        # a dead session registered claiming a renderer.
        _sessions.pop(renderer.udn, None)
        try:
            await session.stop()
        except Exception:
            pass
        raise
    return session


def get_session(udn: str) -> QueueSession | None:
    """Get active session for a renderer."""
    return _sessions.get(udn)


def get_all_sessions() -> dict[str, QueueSession]:
    """Get all active sessions."""
    return dict(_sessions)
