"""Tests for renfield-mcp-dlna MCP server."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from renfield_mcp_dlna import control_point as cp_module
from renfield_mcp_dlna import discovery, queue_manager, server
from renfield_mcp_dlna.backends import avtransport
from renfield_mcp_dlna.backends.avtransport import AvTransportBackend
from renfield_mcp_dlna.control_point import ControlPoint
from renfield_mcp_dlna.didl import build_didl_metadata
from renfield_mcp_dlna.discovery import DlnaRenderer
from renfield_mcp_dlna.queue_manager import QueueSession, Track


def _connected_backend(renderer=None, dmr=None):
    """An AvTransportBackend wired to a mock _dmr (post-refactor device I/O
    lives on the backend, not the session)."""
    backend = AvTransportBackend(renderer or _make_renderer())
    backend._dmr = dmr if dmr is not None else MagicMock()
    return backend


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_renderer(
    name: str = "HiFiBerry Garten",
    udn: str = "uuid:test-1234",
    supports_next: bool = True,
) -> DlnaRenderer:
    return DlnaRenderer(
        name=name,
        udn=udn,
        location="http://192.168.1.100:49152/description.xml",
        supports_next=supports_next,
        av_transport_control_url="http://192.168.1.100:49152/AVTransport/control",
        rendering_control_url="http://192.168.1.100:49152/RenderingControl/control",
        base_url="http://192.168.1.100:49152",
    )


def _make_tracks(count: int = 3) -> list[Track]:
    return [
        Track(
            url=f"http://jellyfin.local:8096/Audio/{i}/stream.flac",
            title=f"Track {i + 1}",
            artist="Test Artist",
            album="Test Album",
        )
        for i in range(count)
    ]


_RC_TYPE = "urn:schemas-upnp-org:service:RenderingControl:1"


def _mock_dmr_with_rc(actions=("GetVolume", "SetVolume", "GetMute", "SetMute"),
                      current_volume=None, scale_max=100):
    """Build a mock _dmr exposing a RenderingControl service for direct-action
    volume/mute. Records calls in the returned `calls` dict keyed by action name."""
    calls: dict = {}
    rc = MagicMock()
    rc.actions = list(actions)

    def _action(name):
        act = MagicMock()

        async def _call(**kw):
            calls.setdefault(name, []).append(kw)
            if name == "GetVolume":
                return {"CurrentVolume": current_volume}
            if name == "GetMute":
                return {"CurrentMute": False}
            return {}

        act.async_call = AsyncMock(side_effect=_call)
        sv = MagicMock()
        sv.max_value = scale_max
        act.argument.return_value.related_state_variable = sv
        return act

    rc.action.side_effect = _action
    dmr = MagicMock()
    dmr.device.services = {_RC_TYPE: rc}
    return dmr, calls


# ---------------------------------------------------------------------------
# DIDL-Lite Tests
# ---------------------------------------------------------------------------

class TestBuildDidlMetadata:
    def test_minimal(self):
        xml = build_didl_metadata("http://example.com/track.flac")
        assert "http://example.com/track.flac" in xml
        assert "Unknown" in xml  # default title
        assert "DIDL-Lite" in xml

    def test_full_metadata(self):
        xml = build_didl_metadata(
            url="http://example.com/track.flac",
            title="Cold as Ice",
            artist="Foreigner",
            album="The Very Best of Foreigner",
            mime_type="audio/flac",
        )
        assert "Cold as Ice" in xml
        assert "Foreigner" in xml
        assert "The Very Best of Foreigner" in xml
        assert "audio/flac" in xml

    def test_custom_mime_type(self):
        xml = build_didl_metadata(
            url="http://example.com/track.mp3",
            mime_type="audio/mpeg",
        )
        assert "audio/mpeg" in xml

    def test_returns_valid_xml(self):
        from xml.etree import ElementTree as ET

        xml = build_didl_metadata("http://example.com/track.flac", title="Test")
        # Should parse without error
        ET.fromstring(xml)


# ---------------------------------------------------------------------------
# Discovery Tests
# ---------------------------------------------------------------------------

class TestFindRenderer:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        discovery._renderer_cache = []
        discovery._cache_time = 0
        yield
        discovery._renderer_cache = []
        discovery._cache_time = 0

    async def test_exact_match(self):
        renderers = [_make_renderer(name="HiFiBerry Garten")]
        with patch.object(discovery, "discover_renderers", new_callable=AsyncMock, return_value=renderers):
            result = await discovery.find_renderer("HiFiBerry Garten")
            assert result is not None
            assert result.name == "HiFiBerry Garten"

    async def test_case_insensitive_match(self):
        renderers = [_make_renderer(name="HiFiBerry Garten")]
        with patch.object(discovery, "discover_renderers", new_callable=AsyncMock, return_value=renderers):
            result = await discovery.find_renderer("hifiberry garten")
            assert result is not None

    async def test_substring_match(self):
        renderers = [_make_renderer(name="HiFiBerry Garten")]
        with patch.object(discovery, "discover_renderers", new_callable=AsyncMock, return_value=renderers):
            result = await discovery.find_renderer("Garten")
            assert result is not None

    async def test_not_found(self):
        renderers = [_make_renderer(name="HiFiBerry Garten")]
        with patch.object(discovery, "discover_renderers", new_callable=AsyncMock, return_value=renderers):
            result = await discovery.find_renderer("Nonexistent")
            assert result is None

    async def test_exact_match_preferred_over_substring(self):
        renderers = [
            _make_renderer(name="Samsung TV", udn="uuid:tv"),
            _make_renderer(name="Samsung TV Living Room", udn="uuid:tv-lr"),
        ]
        with patch.object(discovery, "discover_renderers", new_callable=AsyncMock, return_value=renderers):
            result = await discovery.find_renderer("Samsung TV")
            assert result is not None
            assert result.udn == "uuid:tv"


# ---------------------------------------------------------------------------
# Server Tool Tests
# ---------------------------------------------------------------------------

class TestListRenderers:
    async def test_returns_renderers(self):
        renderers = [
            _make_renderer(name="HiFiBerry Garten", supports_next=True),
            _make_renderer(name="Linn DS", udn="uuid:linn", supports_next=False),
        ]
        with patch.object(discovery, "discover_renderers", new_callable=AsyncMock, return_value=renderers):
            result = await server.list_renderers()
            assert result["total"] == 2
            assert result["renderers"][0]["name"] == "HiFiBerry Garten"
            assert result["renderers"][0]["supports_queue"] is True
            assert result["renderers"][1]["supports_queue"] is False

    async def test_empty_network(self):
        with patch.object(discovery, "discover_renderers", new_callable=AsyncMock, return_value=[]):
            result = await server.list_renderers()
            assert result["total"] == 0
            assert result["renderers"] == []


class TestPlayTracks:
    async def test_renderer_not_found(self):
        with patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=None):
            result = await server.play_tracks("Nonexistent", "[]")
            assert result["success"] is False
            assert "error" in result

    async def test_invalid_json(self):
        renderer = _make_renderer()
        with patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer):
            result = await server.play_tracks("HiFiBerry", "not json")
            assert result["success"] is False
            assert "error" in result
            assert "Invalid tracks JSON" in result["error"]

    async def test_empty_tracks(self):
        renderer = _make_renderer()
        with patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer):
            result = await server.play_tracks("HiFiBerry", "[]")
            assert result["success"] is False
            assert "error" in result
            assert "non-empty" in result["error"]

    async def test_track_missing_url(self):
        renderer = _make_renderer()
        with patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer):
            result = await server.play_tracks("HiFiBerry", '[{"title": "No URL"}]')
            assert result["success"] is False
            assert "error" in result
            assert "url" in result["error"]

    async def test_successful_playback(self):
        renderer = _make_renderer()
        tracks_json = json.dumps([
            {"url": "http://jellyfin/track1.flac", "title": "Track 1", "artist": "Artist"},
            {"url": "http://jellyfin/track2.flac", "title": "Track 2", "artist": "Artist"},
        ])

        mock_session = MagicMock(spec=QueueSession)
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "play_tracks", new_callable=AsyncMock, return_value=mock_session),
        ):
            result = await server.play_tracks("HiFiBerry", tracks_json)
            assert result.get("success") is True
            assert result["total_tracks"] == 2
            assert result["now_playing"]["title"] == "Track 1"


class TestStop:
    async def test_renderer_not_found(self):
        with patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=None):
            result = await server.stop("Nonexistent")
            assert result["success"] is False
            assert "error" in result

    async def test_no_active_session(self):
        renderer = _make_renderer()
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=None),
        ):
            result = await server.stop("HiFiBerry")
            assert result["success"] is False
            assert "error" in result
            assert "No active playback" in result["error"]

    async def test_successful_stop(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.stop = AsyncMock()
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.stop("HiFiBerry")
            assert result.get("success") is True
            mock_session.stop.assert_awaited_once()


class TestNextTrack:
    async def test_successful_next(self):
        renderer = _make_renderer()
        track = Track(url="http://jellyfin/track2.flac", title="Track 2", artist="Artist")
        mock_session = MagicMock(spec=QueueSession)
        mock_session.next = AsyncMock(return_value=track)
        mock_session.current_index = 1
        mock_session.tracks = _make_tracks(3)
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.next_track("HiFiBerry")
            assert result.get("success") is True
            assert result["now_playing"]["title"] == "Track 2"

    async def test_at_last_track(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.next = AsyncMock(return_value=None)
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.next_track("HiFiBerry")
            assert result["success"] is False
            assert "error" in result
            assert "last track" in result["error"]


class TestPreviousTrack:
    async def test_successful_previous(self):
        renderer = _make_renderer()
        track = Track(url="http://jellyfin/track1.flac", title="Track 1", artist="Artist")
        mock_session = MagicMock(spec=QueueSession)
        mock_session.previous = AsyncMock(return_value=track)
        mock_session.current_index = 0
        mock_session.tracks = _make_tracks(3)
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.previous_track("HiFiBerry")
            assert result.get("success") is True

    async def test_at_first_track(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.previous = AsyncMock(return_value=None)
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.previous_track("HiFiBerry")
            assert result["success"] is False
            assert "error" in result
            assert "first track" in result["error"]


class TestGetStatus:
    async def test_no_session(self):
        renderer = _make_renderer()
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=None),
        ):
            result = await server.get_status("HiFiBerry")
            assert result["state"] == "idle"

    async def test_active_session(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.status.return_value = {
            "renderer": "HiFiBerry Garten",
            "state": "playing",
            "track": 2,
            "total_tracks": 10,
            "title": "Cold as Ice",
            "artist": "Foreigner",
            "album": "The Very Best",
        }
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.get_status("HiFiBerry")
            assert result["state"] == "playing"
            assert result["track"] == 2
            assert result["title"] == "Cold as Ice"


class TestSetVolume:
    async def test_successful_volume(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.set_volume = AsyncMock()
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.set_volume("HiFiBerry", 75)
            assert result.get("success") is True
            assert result["volume"] == 75
            mock_session.set_volume.assert_awaited_once_with(75)

    async def test_volume_clamped(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.set_volume = AsyncMock()
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.set_volume("HiFiBerry", 150)
            assert result["volume"] == 100

            result = await server.set_volume("HiFiBerry", -10)
            assert result["volume"] == 0


class TestGetVolume:
    async def test_returns_cached_volume(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.get_volume = AsyncMock(return_value=42)
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.get_volume("HiFiBerry")
            assert result.get("success") is True
            assert result["volume"] == 42
            mock_session.get_volume.assert_awaited_once_with()

    async def test_volume_none_when_unreportable(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.get_volume = AsyncMock(return_value=None)
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.get_volume("HiFiBerry")
            assert result.get("success") is True
            assert result["volume"] is None

    async def test_no_active_session(self):
        renderer = _make_renderer()
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=None),
        ):
            result = await server.get_volume("HiFiBerry")
            assert result.get("success") is False
            assert "No active playback" in result["error"]

    async def test_renderer_not_found(self):
        with patch.object(
            discovery, "find_renderer", new_callable=AsyncMock, return_value=None
        ):
            result = await server.get_volume("Unknown")
            assert result.get("success") is False
            assert "not found" in result["error"]


class TestSetMute:
    async def test_mute(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.set_mute = AsyncMock()
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.set_mute("HiFiBerry", True)
            assert result.get("success") is True
            assert result["muted"] is True
            mock_session.set_mute.assert_awaited_once_with(True)

    async def test_unmute(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.set_mute = AsyncMock()
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.set_mute("HiFiBerry", False)
            assert result.get("success") is True
            assert result["muted"] is False
            mock_session.set_mute.assert_awaited_once_with(False)

    async def test_no_active_session(self):
        renderer = _make_renderer()
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=None),
        ):
            result = await server.set_mute("HiFiBerry", True)
            assert result.get("success") is False
            assert "No active playback" in result["error"]

    async def test_renderer_not_found(self):
        with patch.object(
            discovery, "find_renderer", new_callable=AsyncMock, return_value=None
        ):
            result = await server.set_mute("Unknown", True)
            assert result.get("success") is False
            assert "not found" in result["error"]

    async def test_set_mute_failure(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.set_mute = AsyncMock(side_effect=RuntimeError("upnp error"))
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.set_mute("HiFiBerry", True)
            assert result.get("success") is False
            assert "Failed to set mute" in result["error"]


# ---------------------------------------------------------------------------
# QueueSession Unit Tests
# ---------------------------------------------------------------------------

class TestQueueSessionStatusFields:
    def test_status_returns_correct_info(self):
        renderer = _make_renderer()
        tracks = _make_tracks(5)
        session = QueueSession(renderer, tracks)
        session.current_index = 2

        status = session.status()
        assert status["renderer"] == "HiFiBerry Garten"
        assert status["track"] == 3
        assert status["total_tracks"] == 5
        assert status["title"] == "Track 3"

    def test_status_with_no_dmr(self):
        renderer = _make_renderer()
        tracks = _make_tracks(1)
        session = QueueSession(renderer, tracks)

        status = session.status()
        assert status["state"] == "stopped"


class TestTrackDataclass:
    def test_defaults(self):
        track = Track(url="http://example.com/track.flac")
        assert track.url == "http://example.com/track.flac"
        assert track.title == ""
        assert track.artist == ""
        assert track.album == ""
        assert track.art_url == ""
        assert track.media_type == "audio"

    def test_full_track(self):
        track = Track(
            url="http://example.com/track.flac",
            title="Cold as Ice",
            artist="Foreigner",
            album="The Very Best",
            art_url="http://example.com/art.jpg",
        )
        assert track.title == "Cold as Ice"
        assert track.artist == "Foreigner"

    def test_video_media_type(self):
        track = Track(url="http://example.com/video.mp4", media_type="video")
        assert track.media_type == "video"


# ---------------------------------------------------------------------------
# Video DIDL-Lite Tests
# ---------------------------------------------------------------------------

class TestBuildVideoDidlMetadata:
    def test_valid_video_xml(self):
        from renfield_mcp_dlna.didl import build_video_didl_metadata
        xml = build_video_didl_metadata("http://jellyfin/Videos/m1/stream")
        assert "DIDL-Lite" in xml
        assert "http://jellyfin/Videos/m1/stream" in xml
        assert "video/mp4" in xml
        assert "object.item.videoItem.movie" in xml

    def test_video_with_title(self):
        from renfield_mcp_dlna.didl import build_video_didl_metadata
        xml = build_video_didl_metadata(
            "http://jellyfin/Videos/m1/stream",
            title="Interstellar",
        )
        assert "Interstellar" in xml

    def test_video_with_custom_mime_type(self):
        from renfield_mcp_dlna.didl import build_video_didl_metadata
        xml = build_video_didl_metadata(
            "http://jellyfin/Videos/m1/stream",
            title="Interstellar",
            mime_type="video/x-matroska",
        )
        assert "video/x-matroska" in xml

    def test_video_returns_valid_xml(self):
        from xml.etree import ElementTree as ET
        from renfield_mcp_dlna.didl import build_video_didl_metadata
        xml = build_video_didl_metadata("http://example.com/video.mp4", title="Test")
        ET.fromstring(xml)


# ---------------------------------------------------------------------------
# Video Track Playback Tests
# ---------------------------------------------------------------------------

class TestPlayTracksVideo:
    async def test_video_track_parsed(self):
        """Video track with media_type='video' is parsed correctly."""
        renderer = _make_renderer()
        tracks_json = json.dumps([
            {"url": "http://jellyfin/Videos/m1/stream", "title": "Interstellar", "media_type": "video"},
        ])

        mock_session = MagicMock(spec=QueueSession)
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "play_tracks", new_callable=AsyncMock, return_value=mock_session) as mock_play,
        ):
            result = await server.play_tracks("HiFiBerry", tracks_json)
            assert result.get("success") is True
            tracks_passed = mock_play.call_args.args[1]
            assert tracks_passed[0].media_type == "video"

    async def test_backward_compat_no_media_type(self):
        """Tracks without media_type field default to 'audio'."""
        renderer = _make_renderer()
        tracks_json = json.dumps([
            {"url": "http://jellyfin/Audio/a1/stream", "title": "Song 1"},
        ])

        mock_session = MagicMock(spec=QueueSession)
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "play_tracks", new_callable=AsyncMock, return_value=mock_session) as mock_play,
        ):
            result = await server.play_tracks("HiFiBerry", tracks_json)
            assert result.get("success") is True
            tracks_passed = mock_play.call_args.args[1]
            assert tracks_passed[0].media_type == "audio"


# ---------------------------------------------------------------------------
# QueueSession status honesty + playback-start confirmation (issue #3)
# ---------------------------------------------------------------------------

class TestQueueSessionStatus:
    """status() must reflect the renderer's real TransportState, never assume
    'playing' just because a renderer is bound (the bug that hid silent
    playback when the stream 404'd)."""

    def _bound(self, transport_state):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._dmr = MagicMock()  # backend bound to a renderer
        s.backend._transport_state = transport_state
        return s

    def test_stopped_when_unbound(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        assert s.backend.connected is False
        assert s.status()["state"] == "stopped"

    def test_bound_without_event_is_not_playing(self):
        # The regression: bound → always "playing". Now: no event yet → unknown.
        assert self._bound(None).status()["state"] == "unknown"

    def test_reports_real_transport_state(self):
        assert self._bound("PLAYING").status()["state"] == "playing"
        assert self._bound("PAUSED_PLAYBACK").status()["state"] == "paused"
        assert self._bound("STOPPED").status()["state"] == "stopped"
        assert self._bound("NO_MEDIA_PRESENT").status()["state"] == "stopped"
        assert self._bound("TRANSITIONING").status()["state"] == "transitioning"


class TestConfirmPlaybackStarted:
    """start() must surface a renderer that accepted the command but never
    actually played (e.g. a 404 stream → STOPPED/NO_MEDIA_PRESENT)."""

    async def test_returns_when_playing(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._transport_state = "PLAYING"
        await s._confirm_playback_started("Track 1")  # no raise

    async def test_raises_when_stream_unreachable(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._transport_state = "NO_MEDIA_PRESENT"
        with pytest.raises(RuntimeError, match="did not start playback"):
            await s._confirm_playback_started("Track 1")

    async def test_raises_on_stopped(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._transport_state = "STOPPED"
        with pytest.raises(RuntimeError, match="did not start playback"):
            await s._confirm_playback_started("Track 1")

    async def test_no_false_fail_when_no_event(self, monkeypatch):
        # A renderer that simply hasn't emitted an event yet must not be failed.
        monkeypatch.setattr(queue_manager, "_PLAYBACK_CONFIRM_TIMEOUT", 0.0)
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._transport_state = None
        await s._confirm_playback_started("Track 1")  # warns, no raise

    async def test_confirms_via_poll_when_no_event(self):
        # Event-silent renderer (HiFiBerryOS): no LAST_CHANGE event ever fires,
        # but an active GetTransportInfo poll reports PLAYING → confirmed.
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._transport_state = None
        s.backend.query_transport_state = AsyncMock(return_value="PLAYING")
        await s._confirm_playback_started("Track 1")  # no raise
        s.backend.query_transport_state.assert_awaited()

    async def test_poll_detects_dead_renderer(self):
        # Poll reveals the renderer never started (404 stream → STOPPED).
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._transport_state = None
        s.backend.query_transport_state = AsyncMock(return_value="STOPPED")
        with pytest.raises(RuntimeError, match="did not start playback"):
            await s._confirm_playback_started("Track 1")


class TestQueryTransportState:
    """_query_transport_state actively polls GetTransportInfo; refresh_state
    feeds that into the cached state so status() is accurate on event-silent
    renderers."""

    async def test_query_returns_normalized_state(self):
        dmr = MagicMock()
        dmr.async_update = AsyncMock()
        dmr.transport_state = MagicMock(value="PLAYING")
        b = _connected_backend(dmr=dmr)
        assert await b.query_transport_state() == "PLAYING"
        dmr.async_update.assert_awaited()

    async def test_query_normalizes_real_transport_state_enum(self):
        # The real DmrDevice yields a TransportState(str, Enum) member whose
        # str() is "TransportState.PLAYING" — only .value gives "PLAYING".
        # This guards against a regression to str(ts).upper().
        from async_upnp_client.profiles.dlna import TransportState

        dmr = MagicMock()
        dmr.async_update = AsyncMock()
        dmr.transport_state = TransportState.PLAYING
        b = _connected_backend(dmr=dmr)
        assert await b.query_transport_state() == "PLAYING"

    async def test_query_bounded_by_timeout(self, monkeypatch):
        # A hung renderer must not block get_status: async_update is wrapped in
        # asyncio.wait_for. With no known transport_state, the poll returns None
        # within budget rather than waiting out the hang.
        monkeypatch.setattr(avtransport, "_TRANSPORT_POLL_TIMEOUT", 0.01)

        async def _hang():
            await asyncio.sleep(1.0)

        dmr = MagicMock()
        dmr.async_update = _hang
        dmr.transport_state = None
        b = _connected_backend(dmr=dmr)
        assert await b.query_transport_state() is None

    async def test_query_returns_none_when_unbound(self):
        b = AvTransportBackend(_make_renderer())
        assert b.connected is False
        assert await b.query_transport_state() is None

    async def test_query_returns_none_on_error_without_known_state(self):
        dmr = MagicMock()
        dmr.async_update = AsyncMock(side_effect=RuntimeError("upnp timeout"))
        dmr.transport_state = None
        b = _connected_backend(dmr=dmr)
        assert await b.query_transport_state() is None

    async def test_query_falls_back_to_subscription_state_on_error(self):
        # Even if the active refresh raises, the lib keeps transport_state fresh
        # from the event subscription — report it instead of blanking to None.
        from async_upnp_client.profiles.dlna import TransportState

        dmr = MagicMock()
        dmr.async_update = AsyncMock(side_effect=RuntimeError("upnp timeout"))
        dmr.transport_state = TransportState.PLAYING
        b = _connected_backend(dmr=dmr)
        assert await b.query_transport_state() == "PLAYING"

    async def test_refresh_state_updates_status(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._dmr = MagicMock()
        s.backend.query_transport_state = AsyncMock(return_value="PLAYING")
        await s.refresh_state()
        assert s.backend.transport_state == "PLAYING"
        assert s.status()["state"] == "playing"

    async def test_refresh_state_keeps_last_known_on_failed_poll(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._dmr = MagicMock()
        s.backend._transport_state = "PLAYING"
        s.backend.query_transport_state = AsyncMock(return_value=None)
        await s.refresh_state()
        assert s.backend.transport_state == "PLAYING"  # untouched


class _SV:
    """Minimal stand-in for async_upnp_client's UpnpStateVariable."""

    def __init__(self, name, value):
        self.name = name
        self.value = value


class TestOnEvent:
    """async_upnp_client delivers LAST_CHANGE as a LIST of UpnpStateVariable
    objects — the old code called .get() on it and crashed, so TransportState
    was never captured (status stuck on 'unknown') and gapless/auto-advance
    detection silently never fired."""

    def test_list_of_state_variables_sets_transport_state(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._dmr = MagicMock()
        s.backend._handle_raw_event(
            MagicMock(), [_SV("TransportState", "PLAYING"), _SV("TransportStatus", "OK")]
        )
        assert s.backend.transport_state == "PLAYING"
        assert s.status()["state"] == "playing"

    def test_dict_shape_tolerated(self):
        b = _connected_backend()
        b._handle_raw_event(MagicMock(), {"TransportState": "PAUSED_PLAYBACK"})
        assert b.transport_state == "PAUSED_PLAYBACK"

    def test_empty_event_is_noop(self):
        b = AvTransportBackend(_make_renderer())
        b._handle_raw_event(MagicMock(), [])  # must not raise
        b._handle_raw_event(MagicMock(), None)
        assert b.transport_state is None

    def test_list_without_transport_state_leaves_state(self):
        b = _connected_backend()
        b._transport_state = "PLAYING"
        b._handle_raw_event(MagicMock(), [_SV("Volume", 28), _SV("Mute", False)])
        assert b.transport_state == "PLAYING"  # untouched by unrelated vars


class TestVolumeCache:
    """get_volume reads a cached value (kept fresh by _on_event / set_volume)
    and only round-trips to the renderer via async_update when the cache is
    empty."""

    def test_on_event_caches_volume(self):
        b = AvTransportBackend(_make_renderer())
        b._dmr, _ = _mock_dmr_with_rc(scale_max=100)
        b._handle_raw_event(MagicMock(), [_SV("Volume", 28)])
        assert b._volume == 28

    def test_on_event_bad_volume_ignored(self):
        b = AvTransportBackend(_make_renderer())
        b._dmr, _ = _mock_dmr_with_rc()
        b._handle_raw_event(MagicMock(), [_SV("Volume", "not-a-number")])
        assert b._volume is None

    async def test_on_event_volume_normalized_to_percent_on_scaled_renderer(self):
        """A Volume event on a 0-255 renderer must cache/return percent (50),
        not the raw event value (128)."""
        b = AvTransportBackend(_make_renderer())
        b._dmr, calls = _mock_dmr_with_rc(scale_max=255)
        b._handle_raw_event(MagicMock(), [_SV("Volume", 128)])
        assert b._volume == 50
        assert await b.get_volume() == 50
        assert "GetVolume" not in calls  # served from the normalized cache

    async def test_set_volume_direct_rc_raw_and_caches(self):
        b = AvTransportBackend(_make_renderer())
        b._dmr, calls = _mock_dmr_with_rc(scale_max=100)
        await b.set_volume(55)
        assert b._volume == 55
        # raw 0-100 SetVolume (NOT a 0.0-1.0 fraction, NOT scaled by a bogus max)
        assert calls["SetVolume"][0] == {"InstanceID": 0, "Channel": "Master", "DesiredVolume": 55}

    async def test_set_volume_bogus_range_does_not_blast(self):
        """Linn advertises max 2^31-1; we must treat it as 0-100, NOT send ~858M."""
        b = AvTransportBackend(_make_renderer())
        b._dmr, calls = _mock_dmr_with_rc(scale_max=2147483647)
        await b.set_volume(40)
        assert calls["SetVolume"][0]["DesiredVolume"] == 40  # raw 40, not 858993459

    async def test_set_volume_scales_when_range_is_sane_nonstandard(self):
        b = AvTransportBackend(_make_renderer())
        b._dmr, calls = _mock_dmr_with_rc(scale_max=255)
        await b.set_volume(100)
        assert calls["SetVolume"][0]["DesiredVolume"] == 255  # 100% of a 0-255 range

    async def test_set_mute_direct_rc_when_supported(self):
        b = AvTransportBackend(_make_renderer())
        b._dmr, calls = _mock_dmr_with_rc()
        await b.set_mute(True)
        assert calls["SetMute"][0] == {"InstanceID": 0, "Channel": "Master", "DesiredMute": True}

    async def test_set_mute_clear_error_when_action_absent(self):
        b = AvTransportBackend(_make_renderer())
        b._dmr, calls = _mock_dmr_with_rc(actions=("GetVolume", "SetVolume"))  # no SetMute
        with pytest.raises(RuntimeError, match="does not support mute"):
            await b.set_mute(True)
        assert "SetMute" not in calls

    async def test_get_volume_returns_cache_without_polling(self):
        b = AvTransportBackend(_make_renderer())
        b._dmr, calls = _mock_dmr_with_rc(current_volume=99)
        b._volume = 33
        assert await b.get_volume() == 33
        assert "GetVolume" not in calls  # cache hit, no RC read

    async def test_get_volume_falls_back_to_direct_rc(self):
        b = AvTransportBackend(_make_renderer())
        b._dmr, calls = _mock_dmr_with_rc(current_volume=40, scale_max=100)
        assert b._volume is None
        assert await b.get_volume() == 40
        assert "GetVolume" in calls
        assert b._volume == 40  # now cached

    async def test_get_volume_none_when_rc_returns_none(self):
        b = AvTransportBackend(_make_renderer())
        b._dmr, calls = _mock_dmr_with_rc(current_volume=None)
        assert await b.get_volume() is None

    async def test_get_volume_none_when_no_dmr(self):
        b = AvTransportBackend(_make_renderer())
        assert b.connected is False
        assert await b.get_volume() is None


class TestStopEventGuards:
    """Fixing _on_event activated the previously-dead STOPPED branches
    (auto-advance + queue-finished). A transient pre-playback STOPPED must not
    skip track 1 / tear down the session, and duplicate STOPPED events must not
    double-advance."""

    async def test_transient_stop_before_play_does_not_advance_or_cleanup(self):
        s = QueueSession(_make_renderer(supports_next=False), _make_tracks(3))
        s._auto_advance = AsyncMock()
        s._cleanup = AsyncMock()
        s._on_transport_event("STOPPED", None)  # never reached PLAYING
        await asyncio.sleep(0)
        s._auto_advance.assert_not_awaited()
        s._cleanup.assert_not_awaited()
        assert s._advancing is False

    async def test_stop_after_play_advances_once(self):
        s = QueueSession(_make_renderer(supports_next=False), _make_tracks(3))
        s._auto_advance = AsyncMock()
        s._on_transport_event("PLAYING", None)
        s._on_transport_event("STOPPED", None)
        assert s._advancing is True  # set before scheduling
        await asyncio.sleep(0)
        s._auto_advance.assert_awaited_once()

    async def test_duplicate_stop_advances_only_once(self):
        s = QueueSession(_make_renderer(supports_next=False), _make_tracks(3))
        s._auto_advance = AsyncMock()
        s._on_transport_event("PLAYING", None)
        s._on_transport_event("STOPPED", None)
        s._on_transport_event("STOPPED", None)  # duplicate — guarded
        await asyncio.sleep(0)
        s._auto_advance.assert_awaited_once()

    async def test_transient_stop_single_track_does_not_cleanup(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._cleanup = AsyncMock()
        s._on_transport_event("STOPPED", None)  # never played
        await asyncio.sleep(0)
        s._cleanup.assert_not_awaited()

    async def test_stop_after_play_last_track_cleans_up(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._cleanup = AsyncMock()
        s._on_transport_event("PLAYING", None)
        s._on_transport_event("STOPPED", None)
        await asyncio.sleep(0)
        s._cleanup.assert_awaited_once()

    async def test_stopped_right_after_transitioning_is_not_track_end(self):
        # Regression (code review issue #1): the played-gate reads the PRIOR
        # reported state, not a sticky "has ever played" flag. PLAYING →
        # TRANSITIONING → STOPPED means the STOPPED's prior state is
        # TRANSITIONING (a glitch/buffer), NOT a real track end — so neither
        # auto-advance nor cleanup may fire. A sticky flag would wrongly fire.
        s = QueueSession(_make_renderer(supports_next=False), _make_tracks(3))
        s._auto_advance = AsyncMock()
        s._cleanup = AsyncMock()
        s._on_transport_event("PLAYING", None)
        s._on_transport_event("TRANSITIONING", None)
        s._on_transport_event("STOPPED", None)
        await asyncio.sleep(0)
        s._auto_advance.assert_not_awaited()
        s._cleanup.assert_not_awaited()


# ---------------------------------------------------------------------------
# PlaybackBackend seam (Phase 1 backend extraction)
# ---------------------------------------------------------------------------

class TestBackendSeam:
    """QueueSession owns the queue; the backend owns device I/O. These cover
    the seam between them — selection, event forwarding, and the gapless
    transition reaction (which the device-coupled tests above don't reach)."""

    def test_make_backend_returns_avtransport(self):
        backend = queue_manager._make_backend(_make_renderer())
        assert isinstance(backend, AvTransportBackend)
        assert backend.owns_queue is False

    def test_backend_supports_next_reflects_renderer(self):
        assert queue_manager._make_backend(_make_renderer(supports_next=True)).supports_next
        assert not queue_manager._make_backend(_make_renderer(supports_next=False)).supports_next

    def test_handle_raw_event_forwards_state_and_uri_to_session(self):
        # The backend parses the raw event and forwards (state, uri) to whatever
        # callback connect() wired — here a spy standing in for the session.
        forwarded = []
        b = _connected_backend()
        b._on_event = lambda state, uri: forwarded.append((state, uri))
        b._handle_raw_event(
            MagicMock(),
            [_SV("TransportState", "PLAYING"), _SV("CurrentTrackURI", "http://x/2.flac")],
        )
        assert forwarded == [("PLAYING", "http://x/2.flac")]

    async def test_gapless_transition_adopts_preloaded_index(self):
        # Renderer reports it switched to the preloaded track's URI → the session
        # adopts the preloaded index and kicks off preloading the following one.
        s = QueueSession(_make_renderer(supports_next=True), _make_tracks(3))
        s._preloaded_index = 1
        s._preload_next = AsyncMock()
        s._on_transport_event("PLAYING", s.tracks[1].url)
        await asyncio.sleep(0)
        assert s.current_index == 1
        assert s._preloaded_index is None
        s._preload_next.assert_awaited_once()

    async def test_unrelated_uri_does_not_trigger_transition(self):
        s = QueueSession(_make_renderer(supports_next=True), _make_tracks(3))
        s._preloaded_index = 1
        s._preload_next = AsyncMock()
        s._on_transport_event("PLAYING", "http://other/unknown.flac")
        await asyncio.sleep(0)
        assert s.current_index == 0  # unchanged
        assert s._preloaded_index == 1
        s._preload_next.assert_not_awaited()


# ---------------------------------------------------------------------------
# Device identity + OpenHome detection (feeds backend-class selection)
# ---------------------------------------------------------------------------

_DESC_TMPL = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <friendlyName>{name}</friendlyName>
    <manufacturer>{mfr}</manufacturer>
    <modelName>{model}</modelName>
    <UDN>uuid:dev-1</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <controlURL>/AVTransport/ctrl</controlURL>
        <SCPDURL>/AVTransport/scpd.xml</SCPDURL>
      </service>
      {extra_service}
    </serviceList>
  </device>
</root>"""

_OPENHOME_SVC = """<service>
        <serviceType>urn:av-openhome-org:service:Playlist:1</serviceType>
        <controlURL>/oh/Playlist/ctrl</controlURL>
        <SCPDURL>/oh/Playlist/scpd.xml</SCPDURL>
      </service>"""


def _desc_session(xml: str):
    """An aiohttp-like session whose GET returns the given description XML
    (and an empty SCPD so SetNext detection runs without error)."""
    resp = MagicMock()
    resp.status = 200
    resp.text = AsyncMock(return_value=xml)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


class TestDeviceIdentity:
    async def test_captures_manufacturer_and_model(self):
        xml = _DESC_TMPL.format(
            name="Living Room TV", mfr="Samsung", model="UE55", extra_service=""
        )
        r = await discovery._fetch_device_description(
            _desc_session(xml), "http://1.2.3.4:8080/desc.xml"
        )
        assert r is not None
        assert r.manufacturer == "Samsung"
        assert r.model_name == "UE55"
        assert r.is_openhome is False

    async def test_detects_openhome_playlist_service(self):
        xml = _DESC_TMPL.format(
            name="Linn DS", mfr="Linn", model="Majik", extra_service=_OPENHOME_SVC
        )
        r = await discovery._fetch_device_description(
            _desc_session(xml), "http://1.2.3.4:8080/desc.xml"
        )
        assert r is not None
        assert r.is_openhome is True
        assert r.manufacturer == "Linn"


class TestBackendFactoryOpenHome:
    def test_openhome_renderer_still_gets_avtransport_for_now(self):
        # Seam is wired (is_openhome detected) but OpenHomeBackend is Phase 5;
        # until then OpenHome renderers use the AVTransport they also advertise.
        renderer = _make_renderer()
        renderer.is_openhome = True
        backend = queue_manager._make_backend(renderer)
        assert isinstance(backend, AvTransportBackend)


# ---------------------------------------------------------------------------
# ControlPoint (owns shared UPnP infra + session registry)
# ---------------------------------------------------------------------------

class TestControlPoint:
    def test_session_registry(self):
        cp = ControlPoint()
        sess = object()
        cp.register("udn:1", sess)
        assert cp.get_session("udn:1") is sess
        assert cp.get_all_sessions() == {"udn:1": sess}
        assert cp.get_session("udn:missing") is None
        # get_all_sessions returns a copy, not the live dict
        cp.get_all_sessions()["udn:2"] = object()
        assert cp.get_session("udn:2") is None

    async def test_unregister_closes_infra_when_last_session_leaves(self):
        cp = ControlPoint()
        cp.register("udn:1", object())
        cp.aclose = AsyncMock()
        await cp.unregister("udn:1")
        assert cp.get_all_sessions() == {}
        cp.aclose.assert_awaited_once()

    async def test_unregister_keeps_infra_while_sessions_remain(self):
        cp = ControlPoint()
        cp.register("a", object())
        cp.register("b", object())
        cp.aclose = AsyncMock()
        await cp.unregister("a")
        cp.aclose.assert_not_awaited()
        assert set(cp.get_all_sessions()) == {"b"}

    async def test_ensure_started_idempotent_when_already_started(self):
        cp = ControlPoint()
        cp._notify_server = MagicMock()  # pretend already started
        await cp.ensure_started()  # returns immediately, builds nothing
        assert cp.factory is None  # untouched

    async def test_ensure_started_starts_exactly_once_under_concurrency(self, monkeypatch):
        # Race-safe lazy init (code review issue #2): three concurrent first
        # plays must bind the notify server only once.
        starts = {"n": 0}

        async def _start():
            starts["n"] += 1

        fake_server = MagicMock()
        fake_server.async_start_server = AsyncMock(side_effect=_start)
        fake_server.callback_url = "http://127.0.0.1:0/cb"
        monkeypatch.setattr(cp_module, "AiohttpRequester", MagicMock())
        monkeypatch.setattr(cp_module, "AiohttpNotifyServer", MagicMock(return_value=fake_server))
        monkeypatch.setattr(cp_module, "UpnpEventHandler", MagicMock())
        monkeypatch.setattr(cp_module, "UpnpFactory", MagicMock())
        monkeypatch.setattr(cp_module, "detect_local_ip", lambda: "127.0.0.1")

        cp = ControlPoint()
        await asyncio.gather(cp.ensure_started(), cp.ensure_started(), cp.ensure_started())
        assert starts["n"] == 1
        assert cp.started is True
        assert cp.factory is not None

    def test_detect_local_ip_honours_env_override(self, monkeypatch):
        monkeypatch.setenv("DLNA_LISTEN_IP", "10.0.0.5")
        assert cp_module.detect_local_ip() == "10.0.0.5"


class TestQueueSessionUsesControlPoint:
    def test_session_defaults_to_module_control_point(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        assert s.control_point is queue_manager._default_control_point

    def test_session_accepts_injected_control_point(self):
        cp = ControlPoint()
        s = QueueSession(_make_renderer(), _make_tracks(1), control_point=cp)
        assert s.control_point is cp

    async def test_cleanup_unregisters_from_its_control_point(self):
        cp = ControlPoint()
        cp.aclose = AsyncMock()
        s = QueueSession(_make_renderer(), _make_tracks(1), control_point=cp)
        cp.register(s.renderer.udn, s)
        await s._cleanup()
        assert cp.get_session(s.renderer.udn) is None
