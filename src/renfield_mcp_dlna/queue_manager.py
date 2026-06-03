"""Queue state machine for DLNA renderer playback with UPnP event subscription."""

import asyncio
import logging
import os
import socket
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
        waited = 0.0
        while waited < _PLAYBACK_CONFIRM_TIMEOUT:
            state = (self._transport_state or "").upper()
            if state in _TRANSPORT_OK:
                return
            if state in _TRANSPORT_DEAD:
                raise RuntimeError(
                    f"renderer did not start playback (state={state}); "
                    f"stream may be unreachable: {title}"
                )
            await asyncio.sleep(_PLAYBACK_CONFIRM_INTERVAL)
            waited += _PLAYBACK_CONFIRM_INTERVAL
        logger.warning(
            f"[{self.renderer.name}] playback start unconfirmed for '{title}' "
            f"(last state={self._transport_state})"
        )

    def _on_event(self, service, state_variables) -> None:
        """Handle AVTransport LAST_CHANGE events."""
        if not state_variables:
            return

        transport_state = state_variables.get("TransportState")
        current_uri = state_variables.get("CurrentTrackURI")

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
        # When transport stops and we have more tracks, auto-advance.
        if (
            transport_state == "STOPPED"
            and not self.renderer.supports_next
            and self.current_index < len(self.tracks) - 1
        ):
            logger.info(
                f"[{self.renderer.name}] Track ended (no gapless), advancing..."
            )
            asyncio.create_task(self._auto_advance())
            return

        # Album finished
        if transport_state == "STOPPED" and self.current_index >= len(self.tracks) - 1:
            logger.info(f"[{self.renderer.name}] Queue finished")
            asyncio.create_task(self._cleanup())

    async def _auto_advance(self) -> None:
        """Advance to next track on renderers without SetNext (small gap)."""
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
