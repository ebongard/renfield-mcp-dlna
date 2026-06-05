"""Queue state machine for DLNA renderer playback.

A QueueSession owns the queue (track list + current index) and reacts to
transport events to drive gapless transitions and auto-advance. It delegates
all device I/O — playing a URI, preloading the next, volume/mute, polling — to
a PlaybackBackend (default: AvTransportBackend). See backends/base.py for the
split of responsibility.

Auto-advance / gapless state machine (driven by _on_transport_event):

            play_uri(track 1)
  idle ───────────────────▶ buffering ──PLAYING──▶ playing
   ▲                            │ (_has_played=True)   │
   │                     STOPPED/NO_MEDIA              │ gapless: CurrentTrackURI
   │                     (pre-play, _has_played=False: │   == preloaded.url → adopt
   │                      ignored — no advance/cleanup)│   preloaded index, preload next
   └──── _cleanup ◀── STOPPED (last track, _has_played)│
              ▲                                         │
              └── no-SetNext: STOPPED (_has_played) ───▶ _auto_advance (deduped via _advancing)
"""

import asyncio
import logging
import os
import socket
import time
from dataclasses import dataclass

from async_upnp_client.aiohttp import AiohttpNotifyServer, AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.event_handler import UpnpEventHandler

from .backends import AvTransportBackend, PlaybackBackend
from .backends.base import TRANSPORT_DEAD, TRANSPORT_OK
from .discovery import DlnaRenderer

logger = logging.getLogger(__name__)

# Module-level shared UPnP event infrastructure
_requester: AiohttpRequester | None = None
_notify_server: AiohttpNotifyServer | None = None
_event_handler: UpnpEventHandler | None = None
_factory: UpnpFactory | None = None

# Session registry: renderer UDN → QueueSession
_sessions: dict[str, "QueueSession"] = {}

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


def _make_backend(renderer: DlnaRenderer) -> PlaybackBackend:
    """Select the playback backend for a renderer.

    The OpenHome and Sonos backends slot in here (keyed on advertised services /
    identity) without QueueSession changing — that's the point of the
    PlaybackBackend seam. OpenHome renderers are detected at discovery
    (renderer.is_openhome); until OpenHomeBackend lands (Phase 5) they still use
    AVTransport, which they also advertise.
    """
    if renderer.is_openhome:
        logger.debug(
            f"[{renderer.name}] OpenHome renderer — using AVTransport until "
            f"OpenHomeBackend lands (native Playlist queue pending)"
        )
    return AvTransportBackend(renderer)


class QueueSession:
    """Manages queue playback on a single DLNA renderer.

    Uses UPnP event subscription (LAST_CHANGE, via the backend) for track
    transition detection, with an active-poll fallback for event-silent
    renderers.
    """

    def __init__(self, renderer: DlnaRenderer, tracks: list[Track]):
        self.renderer = renderer
        self.tracks = tracks
        self.current_index = 0
        self.backend: PlaybackBackend = _make_backend(renderer)
        self._preloaded_index: int | None = None
        # True once the *current* track has actually reached a playing state.
        # Gates STOPPED handling so a transient pre-playback STOPPED (buffering
        # window) doesn't skip track 1 or tear down a single-track session.
        # Reset whenever a new track is loaded.
        self._has_played = False
        # Guards the no-SetNext auto-advance against duplicate STOPPED events
        # firing a second _auto_advance before the first incremented the index.
        self._advancing = False

    def _build_metadata(self, track: Track) -> str:
        """Build DIDL-Lite metadata based on track media type."""
        if track.media_type == "video":
            from .didl import build_video_didl_metadata
            return build_video_didl_metadata(track.url, track.title)
        from .didl import build_didl_metadata
        return build_didl_metadata(
            track.url, track.title, track.artist, track.album, track.art_url
        )

    async def start(self) -> None:
        """Connect + subscribe, play track 1, preload track 2."""
        await _ensure_infrastructure()
        assert _factory is not None
        assert _event_handler is not None

        await self.backend.connect(
            self._on_transport_event,
            factory=_factory,
            event_handler=_event_handler,
        )

        track = self.tracks[0]
        await self.backend.play_uri(track.url, track.title, self._build_metadata(track))

        # Verify the renderer actually started — a 404/unreachable stream leaves
        # it STOPPED/NO_MEDIA_PRESENT. Surface that as a failure instead of
        # logging "Playing" and letting status() falsely report success.
        await self._confirm_playback_started(track.title)

        logger.info(f"[{self.renderer.name}] Playing track 1/{len(self.tracks)}: {track.title}")

        if len(self.tracks) > 1 and self.backend.supports_next:
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
            state = (self.backend.transport_state or "").upper()
            # Renderers that don't emit LAST_CHANGE events (e.g. HiFiBerryOS)
            # never populate transport_state — actively poll GetTransportInfo
            # so we can still confirm (or rule out) playback.
            if not state:
                state = (await self.backend.query_transport_state()) or ""
            if state in TRANSPORT_OK:
                return
            if state in TRANSPORT_DEAD:
                raise RuntimeError(
                    f"renderer did not start playback (state={state}); "
                    f"stream may be unreachable: {title}"
                )
            await asyncio.sleep(_PLAYBACK_CONFIRM_INTERVAL)
        logger.warning(
            f"[{self.renderer.name}] playback start unconfirmed for '{title}' "
            f"(last state={self.backend.transport_state})"
        )

    async def refresh_state(self) -> None:
        """Best-effort refresh of the backend's cached TransportState.

        Called by get_status so status() reports the renderer's true state even
        on event-silent renderers."""
        await self.backend.refresh()

    def _on_transport_event(
        self, transport_state: str | None, current_uri: str | None
    ) -> None:
        """React to a parsed transport event forwarded by the backend.

        The backend has already cached transport_state/volume; this method owns
        only the *queue* consequences: gapless transition, auto-advance, and
        end-of-queue cleanup.
        """
        if (transport_state or "").upper() in TRANSPORT_OK:
            self._has_played = True

        # Detect gapless transition: renderer switched to the preloaded track.
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

        # Track end for renderers WITHOUT SetNext: when transport stops AFTER
        # having played and more tracks remain, auto-advance. _advancing dedupes
        # duplicate STOPPED events that would otherwise skip a track.
        if (
            transport_state == "STOPPED"
            and self._has_played
            and not self.backend.supports_next
            and self.current_index < len(self.tracks) - 1
            and not self._advancing
        ):
            logger.info(f"[{self.renderer.name}] Track ended (no gapless), advancing...")
            self._advancing = True
            asyncio.create_task(self._auto_advance())
            return

        # Album finished — only after the last track actually played (guards
        # against a transient pre-playback STOPPED tearing down the session).
        if (
            transport_state == "STOPPED"
            and self._has_played
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
            self._has_played = False
            track = self.tracks[self.current_index]
            try:
                await self.backend.play_uri(
                    track.url, track.title, self._build_metadata(track)
                )
                logger.info(
                    f"[{self.renderer.name}] Auto-advanced to track "
                    f"{self.current_index + 1}/{len(self.tracks)}: {track.title}"
                )
            except Exception as e:
                logger.error(f"[{self.renderer.name}] Auto-advance failed: {e}")
        finally:
            # Re-arm for the next track's STOPPED. The _has_played gate prevents
            # a premature re-advance until the new track actually reaches PLAYING.
            self._advancing = False

    async def _preload_next(self) -> None:
        """Preload the next track via SetNextAVTransportURI."""
        next_idx = self.current_index + 1
        if next_idx >= len(self.tracks) or not self.backend.supports_next:
            return

        track = self.tracks[next_idx]
        try:
            await self.backend.preload_next(
                track.url, track.title, self._build_metadata(track)
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
        self._has_played = False
        track = self.tracks[self.current_index]
        await self.backend.play_uri(track.url, track.title, self._build_metadata(track))
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
        self._has_played = False
        track = self.tracks[self.current_index]
        await self.backend.play_uri(track.url, track.title, self._build_metadata(track))
        await self._preload_next()
        logger.info(
            f"[{self.renderer.name}] Back to track "
            f"{self.current_index + 1}/{len(self.tracks)}: {track.title}"
        )
        return track

    async def stop(self) -> None:
        """Stop playback, unsubscribe, cleanup."""
        await self.backend.disconnect()
        logger.info(f"[{self.renderer.name}] Stopped and cleaned up")
        await self._cleanup()

    async def pause(self) -> None:
        """Pause playback."""
        await self.backend.pause()

    async def resume(self) -> None:
        """Resume playback."""
        await self.backend.play()

    async def set_volume(self, volume: int) -> None:
        """Set playback volume (0-100)."""
        await self.backend.set_volume(volume)

    async def set_mute(self, mute: bool) -> None:
        """Mute (True) or unmute (False)."""
        await self.backend.set_mute(mute)

    async def get_volume(self) -> int | None:
        """Current volume (0-100), or None if unreportable."""
        return await self.backend.get_volume()

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
        because a backend is bound (the old behavior, which lied when the
        stream failed).
        """
        if not self.backend.connected:
            return "stopped"
        state = (self.backend.transport_state or "").upper()
        if state in TRANSPORT_OK:
            return "paused" if state == "PAUSED_PLAYBACK" else "playing"
        if state in TRANSPORT_DEAD:
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
