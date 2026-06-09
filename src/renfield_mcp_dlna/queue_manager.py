"""Queue state machine for DLNA renderer playback.

A QueueSession owns the queue (track list + current index) and reacts to
transport events to drive gapless transitions and auto-advance. It delegates
all device I/O — playing a URI, preloading the next, volume/mute, polling — to
a PlaybackBackend (default: AvTransportBackend). See backends/base.py for the
split of responsibility.

Auto-advance / gapless state machine (driven by _on_transport_event):

            play_uri(track 1)
  idle ───────────────────▶ buffering ──PLAYING──▶ playing
   ▲                            │ (prior state OK)     │
   │                     STOPPED/NO_MEDIA              │ gapless: CurrentTrackURI
   │                     (pre-play, prior state NOT    │   == preloaded.url → adopt
   │                      OK: ignored — no advance)    │   preloaded index, preload next
   └──── _cleanup ◀── STOPPED (last track, played) ────│
              ▲                                         │
              └── no-SetNext: STOPPED (played) ────────▶ _auto_advance (deduped via _advancing)

  "played" = the renderer's PRIOR reported state was PLAYING/PAUSED (_prev_transport_state),
  so a transient STOPPED during buffering — or after a TRANSITIONING — is never read as track-end.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from . import metadata
from .backends import (
    AvTransportBackend,
    OpenHomeBackend,
    PlaybackBackend,
    SonosBackend,
)
from .backends.base import TRANSPORT_DEAD, TRANSPORT_OK
from .control_point import ControlPoint
from .discovery import DlnaRenderer

logger = logging.getLogger(__name__)

# How long start() waits for the renderer to confirm it began playing. Sized so
# the event-silent position-advance path gets at least two GetPositionInfo reads
# even when each poll runs near _TRANSPORT_POLL_TIMEOUT (3s) — otherwise a single
# stuck read could fall through to the lenient branch. Also gives a slow-to-
# buffer stream room to start advancing before we declare a non-start.
_PLAYBACK_CONFIRM_TIMEOUT = 8.0
_PLAYBACK_CONFIRM_INTERVAL = 0.5

# Default control point backing the module-level play_tracks/get_session facade.
# Owns the shared UPnP infra + session registry that used to be module globals.
_default_control_point = ControlPoint()


@dataclass
class Track:
    """A single track in the playback queue."""

    url: str
    title: str = ""
    artist: str = ""
    album: str = ""
    art_url: str = ""
    media_type: str = "audio"  # "audio" or "video"
    # Optional caller hints for metadata negotiation (win over the strategy
    # default): the source's MIME and the DLNA 4th-field (DLNA.ORG_PN/OP/FLAGS).
    mime_type: str = ""
    dlna_features: str = ""


def _make_backend(renderer: DlnaRenderer) -> PlaybackBackend:
    """Select the playback backend for a renderer.

    The OpenHome and Sonos backends slot in here (keyed on advertised services /
    identity) without QueueSession changing — that's the point of the
    PlaybackBackend seam. OpenHome renderers are detected at discovery
    (renderer.is_openhome); until OpenHomeBackend lands (Phase 5) they still use
    AVTransport, which they also advertise.
    """
    # SonosBackend (soco) is PROVISIONAL + needs the optional `soco` dep, so it's
    # opt-in via RENFIELD_SONOS=1. Without it, generic AVTransport gives basic
    # single-device control.
    if renderer.is_sonos and os.getenv("RENFIELD_SONOS") == "1":
        logger.info(f"[{renderer.name}] using SonosBackend (RENFIELD_SONOS=1)")
        return SonosBackend(renderer)

    # OpenHome renderers (Linn) use the native Playlist backend by DEFAULT — it's
    # validated end-to-end on real hardware (discovery + volume + playback) and is
    # the better path (device-owned gapless queue, reliable Volume service vs the
    # bogus-range RenderingControl workaround). RENFIELD_OPENHOME=0 is the safety
    # opt-out back to AVTransport.
    if renderer.is_openhome and os.getenv("RENFIELD_OPENHOME") != "0":
        logger.info(f"[{renderer.name}] using OpenHomeBackend (native Playlist)")
        return OpenHomeBackend(renderer)
    return AvTransportBackend(renderer)


class QueueSession:
    """Manages queue playback on a single DLNA renderer.

    Uses UPnP event subscription (LAST_CHANGE, via the backend) for track
    transition detection, with an active-poll fallback for event-silent
    renderers.
    """

    def __init__(
        self,
        renderer: DlnaRenderer,
        tracks: list[Track],
        control_point: ControlPoint | None = None,
    ):
        self.renderer = renderer
        self.tracks = tracks
        self.control_point = control_point or _default_control_point
        self.current_index = 0
        self.backend: PlaybackBackend = _make_backend(renderer)
        self._preloaded_index: int | None = None
        # Session-side mirror of the renderer's last-reported TransportState,
        # used only for the "did it actually play *before* this STOPPED?" gate.
        # Kept here (not read from the backend) because the backend updates its
        # own cache to the CURRENT state before invoking our callback, so we'd
        # lose the prior value. This reproduces the original per-event logic
        # exactly — a sticky "has ever played" flag would mishandle a
        # TRANSITIONING state landing between PLAYING and a transient STOPPED.
        self._prev_transport_state: str | None = None
        # Guards the no-SetNext auto-advance against duplicate STOPPED events
        # firing a second _auto_advance before the first incremented the index.
        self._advancing = False
        # Negotiated metadata memoised per track URL so preload + re-advance of
        # the same track don't rebuild it (keyed by URL within this session =
        # the (url, UDN) key from the plan, since a session is one renderer).
        self._metadata_cache: dict[str, str] = {}

    def _build_metadata(self, track: Track) -> str:
        """Build (and memoise) DIDL-Lite metadata via the device-family strategy."""
        cached = self._metadata_cache.get(track.url)
        if cached is not None:
            return cached
        built = metadata.build(track, self.renderer)
        self._metadata_cache[track.url] = built
        return built

    async def start(self) -> None:
        """Connect + subscribe, play track 1, preload track 2."""
        await self.control_point.ensure_started()
        assert self.control_point.factory is not None
        assert self.control_point.event_handler is not None

        await self.backend.connect(
            self._on_transport_event,
            factory=self.control_point.factory,
            event_handler=self.control_point.event_handler,
        )

        # Device-owned queue (OpenHome): hand the whole queue over once; the
        # device manages playback + gapless transitions itself.
        if self.backend.owns_queue:
            items = [(t.url, t.title, self._build_metadata(t)) for t in self.tracks]
            await self.backend.load_queue(items, start_index=0)
            logger.info(
                f"[{self.renderer.name}] Loaded device queue: "
                f"{len(self.tracks)} track(s)"
            )
            return

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

        Raises RuntimeError when the renderer accepted the command but did not
        actually start (e.g. a 404/500 media URL, or a wedged renderer) — so a
        non-start is a loud failure, never a false ``success``.

        Two regimes, keyed on whether the renderer emits LAST_CHANGE events:

        * **Event-emitting** renderers (Linn/OpenHome, Sonos, …): the event
          populated ``transport_state`` — trust it (PLAYING → ok, STOPPED → dead).
          No active polling; behaviour is unchanged for these devices.
        * **Event-silent** renderers (e.g. HiFiBerryOS) never populate
          ``transport_state`` AND their *polled* TransportState is unreliable
          (observed reporting PLAYING while silent, and not reporting a clean
          STOPPED on a failed stream). For these the ground truth is the playback
          **position advancing**: poll it and require an increase. A position
          that never advances within the window is a non-start → raise.

        If neither signal is available (event renderer slow to emit, or a backend
        that can't report position) we stay lenient: log a warning and continue
        rather than false-failing.
        """
        # Wall-clock deadline: the poll fallback below can itself take seconds,
        # so summing the sleep interval would badly undercount elapsed time.
        deadline = time.monotonic() + _PLAYBACK_CONFIRM_TIMEOUT
        prev_pos: int | None = None
        positions_seen = 0
        saw_event = False
        while time.monotonic() < deadline:
            evented = (self.backend.transport_state or "").upper()
            if evented:
                # Event-emitting renderer → trust the evented state (unchanged).
                saw_event = True
                if evented in TRANSPORT_OK:
                    return
                if evented in TRANSPORT_DEAD:
                    raise RuntimeError(
                        f"renderer did not start playback (state={evented}); "
                        f"stream may be unreachable: {title}"
                    )
            else:
                # Event-silent renderer → confirm via the position advancing.
                polled, pos = await self.backend.query_playback()
                if (polled or "").upper() in TRANSPORT_DEAD:
                    raise RuntimeError(
                        f"renderer did not start playback (state={polled}); "
                        f"stream may be unreachable: {title}"
                    )
                if pos is not None:
                    positions_seen += 1
                    if prev_pos is not None and pos > prev_pos:
                        return  # ground truth: the stream is being consumed
                    prev_pos = pos
            await asyncio.sleep(_PLAYBACK_CONFIRM_INTERVAL)

        # Timed out. For a renderer that stayed event-SILENT and gave us positions
        # which never advanced, that's a genuine non-start (e.g. an HTTP 500
        # stream) → fail loudly. But if it emitted ANY event (it's alive, just
        # slow to reach a terminal state, e.g. lingering TRANSITIONING), don't
        # fail it on stale silent-phase counters — stay lenient.
        if positions_seen >= 2 and not saw_event:
            raise RuntimeError(
                f"renderer did not start playback (position stuck at {prev_pos}s); "
                f"stream may be unreachable: {title}"
            )
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
        # Did the renderer actually reach a playing state *before* this event?
        # A transient STOPPED/NO_MEDIA during the initial buffering window must
        # NOT be mistaken for track-end (it would skip track 1 or tear down a
        # single-track session). Computed from the PRIOR state, then advance the
        # mirror — exactly as the original did before updating _transport_state.
        played = (self._prev_transport_state or "").upper() in TRANSPORT_OK
        if transport_state:
            self._prev_transport_state = transport_state

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
            and played
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
            # Re-arm for the next track's STOPPED. The played gate (prior state
            # must have been OK) prevents a premature re-advance until the new
            # track actually reaches PLAYING.
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
        if self.backend.owns_queue:
            if not await self.backend.go_next():
                return None
            self.current_index += 1
            return self.tracks[self.current_index]
        self.current_index += 1
        self._preloaded_index = None
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
        if self.backend.owns_queue:
            if not await self.backend.go_previous():
                return None
            self.current_index -= 1
            return self.tracks[self.current_index]
        self.current_index -= 1
        self._preloaded_index = None
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

    async def get_mute(self) -> bool | None:
        """Current mute state, or None if unreportable."""
        return await self.backend.get_mute()

    async def seek(self, position_seconds: int) -> None:
        """Seek to a position (seconds) within the current track."""
        await self.backend.seek(position_seconds)

    async def set_play_mode(self, mode: str) -> None:
        """Set the play mode (normal/repeat_one/repeat_all/shuffle/random)."""
        await self.backend.set_play_mode(mode)

    async def _cleanup(self) -> None:
        """Remove session from the control point (which shuts the shared infra
        down when the last session leaves)."""
        await self.control_point.unregister(self.renderer.udn)

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
        """Return current playback status.

        position/duration/capabilities reflect whatever the backend last
        polled — get_status calls refresh_state() first so they're fresh on
        event-silent renderers. Keys are always present (None/empty when the
        renderer can't report them) so the shape is stable for the client.
        """
        track = self.tracks[self.current_index] if self.tracks else None
        return {
            "renderer": self.renderer.name,
            "state": self._playback_state(),
            "track": self.current_index + 1,
            "total_tracks": len(self.tracks),
            "title": track.title if track else None,
            "artist": track.artist if track else None,
            "album": track.album if track else None,
            "position": self.backend.media_position,
            "duration": self.backend.media_duration,
            "capabilities": self.backend.capabilities,
            "valid_play_modes": sorted(self.backend.valid_play_modes),
        }


async def play_tracks(
    renderer: DlnaRenderer,
    tracks: list[Track],
    control_point: ControlPoint | None = None,
) -> QueueSession:
    """Create and start a new queue session, replacing any existing one."""
    cp = control_point or _default_control_point

    # Serialise the stop-old/start-new swap so two concurrent play requests on
    # the same renderer can't both register a session / race the infra. The lock
    # is acquired only here (start()/stop() don't take it), so no reentrancy.
    async with cp.lock_for(renderer.udn):
        existing = cp.get_session(renderer.udn)
        if existing:
            await existing.stop()

        session = QueueSession(renderer, tracks, control_point=cp)
        cp.register(renderer.udn, session)
        try:
            await session.start()
        except Exception:
            # start() failed (e.g. the renderer never began playback) — don't
            # leave a dead session registered claiming a renderer.
            cp.sessions.pop(renderer.udn, None)
            try:
                await session.stop()
            except Exception:
                pass
            raise
        return session


def get_session(udn: str) -> QueueSession | None:
    """Get active session for a renderer (from the default control point)."""
    return _default_control_point.get_session(udn)  # type: ignore[return-value]


def get_all_sessions() -> dict[str, QueueSession]:
    """Get all active sessions (from the default control point)."""
    return _default_control_point.get_all_sessions()  # type: ignore[return-value]
