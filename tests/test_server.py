"""Tests for renfield-mcp-dlna MCP server."""

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from async_upnp_client.profiles.dlna import PlayMode

from renfield_mcp_dlna import control_point as cp_module
from renfield_mcp_dlna import discovery, mediaserver, queue_manager, server
from renfield_mcp_dlna.backends import avtransport
from renfield_mcp_dlna.backends.avtransport import AvTransportBackend
from renfield_mcp_dlna.backends.openhome import OpenHomeBackend
from renfield_mcp_dlna.backends.sonos import SonosBackend
from renfield_mcp_dlna.control_point import ControlPoint
from renfield_mcp_dlna.didl import build_didl_metadata
from renfield_mcp_dlna.discovery import DlnaRenderer, DlnaServer
from renfield_mcp_dlna.queue_manager import QueueSession, Track


def _make_server(name: str = "Jellyfin", udn: str = "uuid:srv-1") -> DlnaServer:
    return DlnaServer(
        name=name,
        udn=udn,
        location="http://192.168.1.50:8096/desc.xml",
        content_directory_control_url="http://192.168.1.50:8096/cd/control",
        base_url="http://192.168.1.50:8096",
        manufacturer="Jellyfin",
        model_name="Jellyfin Server",
    )


def _didl_obj(obj_id, title, upnp_class, url=None, **extra):
    """A stand-in for a didl_lite object as DmsDevice returns."""
    o = MagicMock()
    o.id = obj_id
    o.title = title
    o.upnp_class = upnp_class
    o.resources = [MagicMock(uri=url)] if url else []
    for k, v in extra.items():
        setattr(o, k, v)
    return o


def _browse_result(objects, total=None):
    from types import SimpleNamespace
    return SimpleNamespace(
        result=objects,
        number_returned=len(objects),
        total_matches=total if total is not None else len(objects),
        update_id=1,
    )


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

    async def test_confirms_via_position_advance_when_no_event(self, monkeypatch):
        # Event-silent renderer (HiFiBerryOS): no LAST_CHANGE event, and its
        # polled TransportState is NOT trusted — confirmation requires the
        # playback POSITION to advance between polls.
        monkeypatch.setattr(queue_manager, "_PLAYBACK_CONFIRM_INTERVAL", 0.01)
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._transport_state = None
        s.backend.query_playback = AsyncMock(side_effect=[("PLAYING", 0), ("PLAYING", 2)])
        await s._confirm_playback_started("Track 1")  # 0 -> 2: confirmed, no raise
        assert s.backend.query_playback.await_count >= 2

    async def test_silent_false_playing_stuck_position_raises(self, monkeypatch):
        # The desync / false-PLAYING case: polled state says PLAYING but the
        # position never advances (no real audio) → must FAIL, not lie success.
        monkeypatch.setattr(queue_manager, "_PLAYBACK_CONFIRM_TIMEOUT", 0.2)
        monkeypatch.setattr(queue_manager, "_PLAYBACK_CONFIRM_INTERVAL", 0.01)
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._transport_state = None
        s.backend.query_playback = AsyncMock(return_value=("PLAYING", 0))  # stuck at 0
        with pytest.raises(RuntimeError, match="position stuck"):
            await s._confirm_playback_started("Track 1")

    async def test_poll_detects_dead_renderer(self):
        # Event-silent renderer whose polled state is a clean STOPPED → fail.
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._transport_state = None
        s.backend.query_playback = AsyncMock(return_value=("STOPPED", None))
        with pytest.raises(RuntimeError, match="did not start playback"):
            await s._confirm_playback_started("Track 1")

    async def test_event_renderer_is_not_position_polled(self):
        # Renderers that DO emit events must NOT be position-polled — the evented
        # PLAYING is trusted directly (per design: poll only the silent ones).
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._transport_state = "PLAYING"
        s.backend.query_playback = AsyncMock()
        await s._confirm_playback_started("Track 1")  # no raise
        s.backend.query_playback.assert_not_awaited()

    async def test_event_after_silent_suppresses_stuck_raise(self, monkeypatch):
        # A renderer that reads as silent+stuck first, then starts emitting a
        # non-terminal event (TRANSITIONING), is ALIVE — the stale silent-phase
        # 'stuck' counters must NOT fire a false 'position stuck' failure.
        monkeypatch.setattr(queue_manager, "_PLAYBACK_CONFIRM_TIMEOUT", 0.3)
        monkeypatch.setattr(queue_manager, "_PLAYBACK_CONFIRM_INTERVAL", 0.01)
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s.backend._transport_state = None
        calls = {"n": 0}

        async def _qp():
            calls["n"] += 1
            if calls["n"] >= 2:
                s.backend._transport_state = "TRANSITIONING"  # renderer begins eventing
            return ("PLAYING", 0)  # silent-phase polled state + stuck position

        s.backend.query_playback = _qp
        await s._confirm_playback_started("Track 1")  # alive → lenient, no raise


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


# (OpenHome factory routing is covered by TestOpenHomeFactoryRouting below.)


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


# ---------------------------------------------------------------------------
# P2: enriched status + get_mute (read-mostly capability surface)
# ---------------------------------------------------------------------------

class TestBackendReadAccessors:
    def test_position_and_duration_from_dmr(self):
        b = _connected_backend()
        b._dmr.media_position = 42
        b._dmr.media_duration = 215
        assert b.media_position == 42
        assert b.media_duration == 215

    def test_position_none_when_unbound(self):
        b = AvTransportBackend(_make_renderer())
        assert b.media_position is None
        assert b.media_duration is None
        assert b.capabilities == {}

    def test_capabilities_from_transport_actions(self):
        b = _connected_backend()
        b._dmr.can_pause = True
        b._dmr.can_seek_rel_time = True
        b._dmr.can_seek_abs_time = False
        b._dmr.can_next = False
        b._dmr.can_previous = True
        caps = b.capabilities
        assert caps == {
            "can_pause": True,
            "can_seek": True,
            "can_next": False,
            "can_previous": True,
        }

    async def test_get_mute_reads_rc(self):
        b = AvTransportBackend(_make_renderer())
        b._dmr, calls = _mock_dmr_with_rc()  # GetMute returns CurrentMute False
        assert await b.get_mute() is False
        assert "GetMute" in calls

    async def test_get_mute_none_when_action_absent(self):
        b = AvTransportBackend(_make_renderer())
        b._dmr, _ = _mock_dmr_with_rc(actions=("GetVolume", "SetVolume"))  # no GetMute
        assert await b.get_mute() is None

    async def test_get_mute_none_when_unbound(self):
        b = AvTransportBackend(_make_renderer())
        assert await b.get_mute() is None


class TestStatusEnrichment:
    def test_status_includes_position_duration_capabilities(self):
        s = QueueSession(_make_renderer(), _make_tracks(2))
        s.backend._dmr = MagicMock()
        s.backend._dmr.media_position = 12
        s.backend._dmr.media_duration = 200
        s.backend._dmr.can_pause = True
        s.backend._dmr.can_seek_rel_time = True
        s.backend._dmr.can_seek_abs_time = False
        s.backend._dmr.can_next = True
        s.backend._dmr.can_previous = False
        status = s.status()
        assert status["position"] == 12
        assert status["duration"] == 200
        assert status["capabilities"]["can_seek"] is True
        assert status["capabilities"]["can_next"] is True

    def test_status_keys_present_even_when_unreportable(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        status = s.status()
        # Stable shape: keys always present, None/empty when unknown.
        assert status["position"] is None
        assert status["duration"] is None
        assert status["capabilities"] == {}


class TestGetMuteTool:
    async def test_returns_mute_state(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.get_mute = AsyncMock(return_value=True)
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.get_mute("HiFiBerry")
            assert result.get("success") is True
            assert result["muted"] is True

    async def test_muted_none_when_unreportable(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.get_mute = AsyncMock(return_value=None)
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.get_mute("HiFiBerry")
            assert result.get("success") is True
            assert result["muted"] is None

    async def test_no_active_session(self):
        renderer = _make_renderer()
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=None),
        ):
            result = await server.get_mute("HiFiBerry")
            assert result.get("success") is False
            assert "No active playback" in result["error"]

    async def test_renderer_not_found(self):
        with patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=None):
            result = await server.get_mute("Unknown")
            assert result.get("success") is False
            assert "not found" in result["error"]


class TestGetStatusEnrichment:
    async def test_status_tool_adds_volume_and_muted(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.refresh_state = AsyncMock()
        mock_session.status.return_value = {"renderer": "HiFiBerry Garten", "state": "playing"}
        mock_session.get_volume = AsyncMock(return_value=44)
        mock_session.get_mute = AsyncMock(return_value=False)
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.get_status("HiFiBerry")
            assert result["state"] == "playing"
            assert result["volume"] == 44
            assert result["muted"] is False
            mock_session.refresh_state.assert_awaited_once()


# ---------------------------------------------------------------------------
# P2: seek + play mode (write actions, capability-gated)
# ---------------------------------------------------------------------------

class TestBackendSeekPlayMode:
    async def test_seek_calls_rel_time_when_supported(self):
        b = _connected_backend()
        b._dmr.can_seek_rel_time = True
        b._dmr.async_seek_rel_time = AsyncMock()
        await b.seek(90)
        b._dmr.async_seek_rel_time.assert_awaited_once_with(timedelta(seconds=90))

    async def test_seek_clamps_negative_to_zero(self):
        b = _connected_backend()
        b._dmr.can_seek_rel_time = True
        b._dmr.async_seek_rel_time = AsyncMock()
        await b.seek(-5)
        b._dmr.async_seek_rel_time.assert_awaited_once_with(timedelta(seconds=0))

    async def test_seek_raises_when_unsupported(self):
        b = _connected_backend()
        b._dmr.can_seek_rel_time = False
        with pytest.raises(RuntimeError, match="does not support seek"):
            await b.seek(10)

    async def test_seek_raises_when_unbound(self):
        b = AvTransportBackend(_make_renderer())
        with pytest.raises(RuntimeError, match="No active playback"):
            await b.seek(10)

    def test_valid_play_modes_normalized_to_lowercase(self):
        b = _connected_backend()
        b._dmr.valid_play_modes = {PlayMode.NORMAL, PlayMode.SHUFFLE, PlayMode.REPEAT_ALL}
        assert b.valid_play_modes == {"normal", "shuffle", "repeat_all"}

    def test_valid_play_modes_empty_when_unbound(self):
        b = AvTransportBackend(_make_renderer())
        assert b.valid_play_modes == set()

    async def test_set_play_mode_valid(self):
        b = _connected_backend()
        b._dmr.valid_play_modes = {PlayMode.NORMAL, PlayMode.SHUFFLE}
        b._dmr.async_set_play_mode = AsyncMock()
        await b.set_play_mode("shuffle")
        b._dmr.async_set_play_mode.assert_awaited_once_with(PlayMode.SHUFFLE)

    async def test_set_play_mode_rejects_unsupported_mode(self):
        b = _connected_backend()
        b._dmr.valid_play_modes = {PlayMode.NORMAL}
        with pytest.raises(RuntimeError, match="does not support play mode"):
            await b.set_play_mode("random")


class TestSeekTool:
    async def test_seek_success(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.seek = AsyncMock()
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.seek("HiFiBerry", 75)
            assert result.get("success") is True
            assert result["position"] == 75
            mock_session.seek.assert_awaited_once_with(75)

    async def test_seek_failure_surfaced(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.seek = AsyncMock(side_effect=RuntimeError("does not support seek"))
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.seek("HiFiBerry", 10)
            assert result.get("success") is False
            assert "Seek failed" in result["error"]

    async def test_seek_no_session(self):
        renderer = _make_renderer()
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=None),
        ):
            result = await server.seek("HiFiBerry", 10)
            assert result.get("success") is False
            assert "No active playback" in result["error"]


class TestSetPlayModeTool:
    async def test_set_play_mode_success(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.set_play_mode = AsyncMock()
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.set_play_mode("HiFiBerry", "Shuffle")
            assert result.get("success") is True
            assert result["play_mode"] == "shuffle"  # normalized
            mock_session.set_play_mode.assert_awaited_once_with("Shuffle")

    async def test_set_play_mode_failure_surfaced(self):
        renderer = _make_renderer()
        mock_session = MagicMock(spec=QueueSession)
        mock_session.set_play_mode = AsyncMock(side_effect=RuntimeError("does not support play mode 'random'"))
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=mock_session),
        ):
            result = await server.set_play_mode("HiFiBerry", "random")
            assert result.get("success") is False
            assert "Failed to set play mode" in result["error"]


# ---------------------------------------------------------------------------
# P4: MediaServer (ContentDirectory) browsing + play_from_server
# ---------------------------------------------------------------------------

class TestMediaServerParsing:
    def test_item_with_resource_is_playable(self):
        obj = _didl_obj("track-1", "Cold as Ice", "object.item.audioItem.musicTrack",
                        url="http://srv/1.flac", artist="Foreigner", album="4")
        d = mediaserver._object_to_dict(obj)
        assert d["type"] == "item"
        assert d["playable"] is True
        assert d["url"] == "http://srv/1.flac"
        assert d["media_type"] == "audio"
        assert d["artist"] == "Foreigner"

    def test_container_is_not_playable(self):
        obj = _didl_obj("album-1", "Greatest Hits", "object.container.album.musicAlbum")
        d = mediaserver._object_to_dict(obj)
        assert d["type"] == "container"
        assert d["playable"] is False
        assert d["url"] == ""

    def test_video_item_media_type(self):
        obj = _didl_obj("v1", "Movie", "object.item.videoItem.movie", url="http://srv/m.mp4")
        assert mediaserver._object_to_dict(obj)["media_type"] == "video"

    def test_descriptor_without_class_is_skipped(self):
        desc = MagicMock(spec=[])  # no upnp_class / id attrs
        assert mediaserver._object_to_dict(desc) is None
        objs = [desc, _didl_obj("i", "t", "object.item.audioItem", url="http://x/a")]
        assert len(mediaserver._parse_objects(objs)) == 1


class TestMediaServerBrowseSearch:
    async def test_browse_returns_parsed_children(self, monkeypatch):
        dms = MagicMock()
        dms.async_browse_direct_children = AsyncMock(return_value=_browse_result([
            _didl_obj("c1", "Albums", "object.container"),
            _didl_obj("t1", "Song", "object.item.audioItem.musicTrack", url="http://srv/s.flac"),
        ], total=2))
        monkeypatch.setattr(mediaserver, "_make_dms", AsyncMock(return_value=dms))
        result = await mediaserver.browse(_make_server(), MagicMock(), "0")
        assert result["total"] == 2
        assert result["items"][0]["type"] == "container"
        assert result["items"][1]["playable"] is True
        dms.async_browse_direct_children.assert_awaited_once()

    async def test_search_requires_capability(self, monkeypatch):
        dms = MagicMock()
        dms.has_search_directory = False
        monkeypatch.setattr(mediaserver, "_make_dms", AsyncMock(return_value=dms))
        with pytest.raises(RuntimeError, match="does not support search"):
            await mediaserver.search(_make_server(), MagicMock(), "ice")

    async def test_search_builds_title_criteria(self, monkeypatch):
        dms = MagicMock()
        dms.has_search_directory = True
        dms.async_search_directory = AsyncMock(return_value=_browse_result([
            _didl_obj("t1", "Cold as Ice", "object.item.audioItem.musicTrack", url="http://srv/s.flac"),
        ]))
        monkeypatch.setattr(mediaserver, "_make_dms", AsyncMock(return_value=dms))
        result = await mediaserver.search(_make_server(), MagicMock(), 'ice"x')
        assert result["returned"] == 1
        criteria = dms.async_search_directory.call_args.args[1]
        assert 'dc:title contains' in criteria
        assert '\\"' in criteria  # embedded quote escaped

    async def test_resolve_playables_container_path(self, monkeypatch):
        dms = MagicMock()
        dms.async_browse_direct_children = AsyncMock(return_value=_browse_result([
            _didl_obj("t1", "A", "object.item.audioItem.musicTrack", url="http://srv/a.flac"),
            _didl_obj("t2", "B", "object.item.audioItem.musicTrack", url="http://srv/b.flac"),
            _didl_obj("c", "sub", "object.container"),  # non-playable, excluded
        ]))
        monkeypatch.setattr(mediaserver, "_make_dms", AsyncMock(return_value=dms))
        playables = await mediaserver.resolve_playables(_make_server(), MagicMock(), "album-1")
        assert [p["url"] for p in playables] == ["http://srv/a.flac", "http://srv/b.flac"]

    async def test_resolve_playables_single_item_fallback(self, monkeypatch):
        dms = MagicMock()
        dms.async_browse_direct_children = AsyncMock(return_value=_browse_result([]))  # no children
        # async_browse_metadata returns a SINGLE DidlObject (not a browse-result
        # wrapper) — mirror the real library so this asserts real behavior.
        dms.async_browse_metadata = AsyncMock(return_value=_didl_obj(
            "t1", "Solo", "object.item.audioItem.musicTrack", url="http://srv/solo.flac",
        ))
        monkeypatch.setattr(mediaserver, "_make_dms", AsyncMock(return_value=dms))
        playables = await mediaserver.resolve_playables(_make_server(), MagicMock(), "t1")
        assert len(playables) == 1
        assert playables[0]["url"] == "http://srv/solo.flac"
        dms.async_browse_metadata.assert_awaited_once()


class TestMediaServerTools:
    async def test_list_servers(self):
        servers = [_make_server(name="Jellyfin"), _make_server(name="MinimServer", udn="uuid:m")]
        with patch.object(discovery, "discover_servers", new_callable=AsyncMock, return_value=servers):
            result = await server.list_servers()
            assert result["total"] == 2
            assert result["servers"][0]["name"] == "Jellyfin"

    async def test_browse_server_success(self):
        with (
            patch.object(discovery, "find_server", new_callable=AsyncMock, return_value=_make_server()),
            patch.object(server, "_content_directory_factory", new_callable=AsyncMock, return_value=MagicMock()),
            patch.object(mediaserver, "browse", new_callable=AsyncMock, return_value={"items": [], "total": 0}),
        ):
            result = await server.browse_server("Jellyfin", "0")
            assert result.get("success") is True
            assert result["total"] == 0

    async def test_browse_server_not_found(self):
        with patch.object(discovery, "find_server", new_callable=AsyncMock, return_value=None):
            result = await server.browse_server("Nope")
            assert result.get("success") is False
            assert "not found" in result["error"]

    async def test_search_server_failure_surfaced(self):
        with (
            patch.object(discovery, "find_server", new_callable=AsyncMock, return_value=_make_server()),
            patch.object(server, "_content_directory_factory", new_callable=AsyncMock, return_value=MagicMock()),
            patch.object(mediaserver, "search", new_callable=AsyncMock, side_effect=RuntimeError("does not support search")),
        ):
            result = await server.search_server("Jellyfin", "ice")
            assert result.get("success") is False
            assert "Search failed" in result["error"]

    async def test_play_from_server_wires_resolved_tracks(self):
        playables = [
            {"url": "http://srv/a.flac", "title": "A", "artist": "X", "album": "Y", "media_type": "audio"},
            {"url": "http://srv/b.flac", "title": "B", "artist": "X", "album": "Y", "media_type": "audio"},
        ]
        with (
            patch.object(discovery, "find_server", new_callable=AsyncMock, return_value=_make_server()),
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=_make_renderer()),
            patch.object(server, "_content_directory_factory", new_callable=AsyncMock, return_value=MagicMock()),
            patch.object(mediaserver, "resolve_playables", new_callable=AsyncMock, return_value=playables),
            patch.object(queue_manager, "play_tracks", new_callable=AsyncMock) as mock_play,
        ):
            result = await server.play_from_server("Jellyfin", "album-1", "HiFiBerry")
            assert result.get("success") is True
            assert result["total_tracks"] == 2
            assert result["now_playing"]["title"] == "A"
            tracks_arg = mock_play.call_args.args[1]
            assert [t.url for t in tracks_arg] == ["http://srv/a.flac", "http://srv/b.flac"]

    async def test_play_from_server_no_playables(self):
        with (
            patch.object(discovery, "find_server", new_callable=AsyncMock, return_value=_make_server()),
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=_make_renderer()),
            patch.object(server, "_content_directory_factory", new_callable=AsyncMock, return_value=MagicMock()),
            patch.object(mediaserver, "resolve_playables", new_callable=AsyncMock, return_value=[]),
        ):
            result = await server.play_from_server("Jellyfin", "empty", "HiFiBerry")
            assert result.get("success") is False
            assert "No playable items" in result["error"]


class TestServerDescriptionParsing:
    async def test_parses_content_directory_server(self):
        xml = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
  <friendlyName>Jellyfin</friendlyName><manufacturer>Jellyfin</manufacturer>
  <modelName>10.x</modelName><UDN>uuid:srv-9</UDN>
  <serviceList><service>
    <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
    <controlURL>/cd/control</controlURL>
  </service></serviceList>
</device></root>"""
        r = await discovery._fetch_server_description(_desc_session(xml), "http://1.2.3.4:8096/d.xml")
        assert r is not None
        assert r.name == "Jellyfin"
        assert r.content_directory_control_url == "http://1.2.3.4:8096/cd/control"

    async def test_renderer_without_content_directory_is_not_a_server(self):
        xml = _DESC_TMPL.format(name="Speaker", mfr="HiFiBerry", model="OS", extra_service="")
        r = await discovery._fetch_server_description(_desc_session(xml), "http://1.2.3.4:9999/d.xml")
        assert r is None


# ---------------------------------------------------------------------------
# Per-UDN lock (serialises concurrent play swaps on one renderer)
# ---------------------------------------------------------------------------

class TestPerUdnLock:
    def test_lock_for_is_stable_per_udn(self):
        cp = ControlPoint()
        a1 = cp.lock_for("uuid:a")
        a2 = cp.lock_for("uuid:a")
        b = cp.lock_for("uuid:b")
        assert a1 is a2  # same UDN → same lock
        assert a1 is not b  # different UDN → different lock

    async def test_concurrent_play_tracks_serialised(self, monkeypatch):
        # Two concurrent play_tracks on the SAME renderer must not interleave the
        # stop-old/start-new swap; exactly one session remains, started twice in
        # series (the 2nd stops the 1st).
        cp = ControlPoint()
        renderer = _make_renderer()
        order = []

        async def _fake_start(self):
            order.append(("start", id(self)))
            await asyncio.sleep(0)  # yield: lets the other task try to interleave

        async def _fake_stop(self):
            order.append(("stop", id(self)))
            await self.control_point.unregister(self.renderer.udn)

        monkeypatch.setattr(QueueSession, "start", _fake_start)
        monkeypatch.setattr(QueueSession, "stop", _fake_stop)

        await asyncio.gather(
            queue_manager.play_tracks(renderer, _make_tracks(1), control_point=cp),
            queue_manager.play_tracks(renderer, _make_tracks(1), control_point=cp),
        )
        # One session left registered; the swap ran without interleaving (the
        # second play stopped the first before starting its own).
        assert len(cp.get_all_sessions()) == 1
        assert order.count(("start", order[0][1])) == 1  # first start not duplicated


# ---------------------------------------------------------------------------
# T7/T8: metadata strategy (caller hints + family-aware protocolInfo) + memoize
# ---------------------------------------------------------------------------

from renfield_mcp_dlna import metadata as md_strategy  # noqa: E402


class TestMetadataStrategy:
    def test_audio_default_keeps_wildcard_4th_field(self):
        # No regression for standard renderers: audio stays "*" unless hinted.
        out = md_strategy.build(Track(url="http://x/a.flac", title="A"), _make_renderer())
        assert "http-get:*:audio/flac:*" in out

    def test_audio_caller_hints_win(self):
        t = Track(url="http://x/a.mp3", title="A", mime_type="audio/mpeg",
                  dlna_features="DLNA.ORG_OP=01")
        out = md_strategy.build(t, _make_renderer())
        assert "http-get:*:audio/mpeg:DLNA.ORG_OP=01" in out

    def test_video_gets_dlna_flags_on_standard_renderer(self):
        t = Track(url="http://x/v.mp4", title="V", media_type="video")
        out = md_strategy.build(t, _make_renderer())
        assert "video/mp4" in out
        assert "DLNA.ORG_OP=01" in out
        assert "DLNA.ORG_FLAGS=" in out

    def test_video_on_tv_family_detected(self):
        tv = _make_renderer(name="Samsung TV", udn="uuid:tv")
        tv.manufacturer = "Samsung"
        assert md_strategy._is_tv(tv) is True
        out = md_strategy.build(Track(url="http://x/v.mp4", media_type="video"), tv)
        assert "DLNA.ORG_FLAGS=" in out

    def test_video_caller_features_override_strategy(self):
        t = Track(url="http://x/v.mkv", media_type="video",
                  mime_type="video/x-matroska", dlna_features="DLNA.ORG_PN=CUSTOM")
        out = md_strategy.build(t, _make_renderer())
        assert "video/x-matroska:DLNA.ORG_PN=CUSTOM" in out

    def test_non_tv_renderer_not_flagged(self):
        assert md_strategy._is_tv(_make_renderer()) is False  # HiFiBerry, no mfr


class TestMetadataMemoization:
    def test_build_metadata_caches_per_url(self):
        s = QueueSession(_make_renderer(), [Track(url="http://x/a.flac", title="A")])
        first = s._build_metadata(s.tracks[0])
        assert s._metadata_cache["http://x/a.flac"] == first

    def test_build_metadata_does_not_rebuild_cached(self):
        s = QueueSession(_make_renderer(), [Track(url="http://x/a.flac", title="A")])
        first = s._build_metadata(s.tracks[0])
        with patch.object(queue_manager.metadata, "build") as mb:
            second = s._build_metadata(s.tracks[0])
            mb.assert_not_called()  # served from cache, no rebuild
        assert second == first


# ---------------------------------------------------------------------------
# T10: OpenHomeBackend (Linn / device-owned queue) — env-gated, provisional
# ---------------------------------------------------------------------------

_OH_PLAYLIST = "urn:av-openhome-org:service:Playlist:1"
_OH_VOLUME = "urn:av-openhome-org:service:Volume:1"


def _mock_openhome_device(insert_ids=(11, 12, 13), volume_max=100,
                          cur_volume=50, cur_mute=False):
    """A mock UpnpDevice exposing OpenHome Playlist + Volume services. Records
    action calls in the returned `calls` dict keyed by action name."""
    calls: dict = {}
    ids = iter(insert_ids)

    def _action(name):
        act = MagicMock()

        async def _call(**kw):
            calls.setdefault(name, []).append(kw)
            if name == "Insert":
                return {"NewId": next(ids)}
            if name == "Characteristics":
                return {"VolumeMax": volume_max}
            if name == "Volume":
                return {"Value": cur_volume}
            if name == "Mute":
                return {"Value": cur_mute}
            return {}

        act.async_call = AsyncMock(side_effect=_call)
        return act

    pl = MagicMock()
    pl.action.side_effect = _action
    vol = MagicMock()
    vol.action.side_effect = _action
    device = MagicMock()
    device.services = {_OH_PLAYLIST: pl, _OH_VOLUME: vol}
    return device, calls


class TestOpenHomeBackend:
    def test_owns_queue_and_supports_next(self):
        b = OpenHomeBackend(_make_renderer())
        assert b.owns_queue is True
        assert b.supports_next is True

    async def test_load_queue_inserts_chained_and_plays(self):
        b = OpenHomeBackend(_make_renderer())
        b._device, calls = _mock_openhome_device(insert_ids=(11, 12))
        await b.load_queue([("u1", "A", "m1"), ("u2", "B", "m2")], start_index=0)
        assert len(calls["Insert"]) == 2
        assert calls["Insert"][0]["AfterId"] == 0       # head
        assert calls["Insert"][1]["AfterId"] == 11      # after the first NewId
        assert calls["SeekId"][0]["Value"] == 11        # start at first track id
        assert "Play" in calls
        assert b._track_ids == [11, 12]
        assert b.transport_state == "PLAYING"

    async def test_go_next_then_previous(self):
        b = OpenHomeBackend(_make_renderer())
        b._device, calls = _mock_openhome_device(insert_ids=(11, 12, 13))
        await b.load_queue([("u", "t", "m")] * 3)
        assert await b.go_next() is True
        assert b._current_index == 1
        assert await b.go_previous() is True
        assert b._current_index == 0
        assert await b.go_previous() is False  # already at start

    async def test_go_next_at_end_is_false(self):
        b = OpenHomeBackend(_make_renderer())
        b._device, _ = _mock_openhome_device(insert_ids=(11,))
        await b.load_queue([("u", "t", "m")])
        assert await b.go_next() is False

    async def test_volume_uses_openhome_service(self):
        b = OpenHomeBackend(_make_renderer())
        b._device, calls = _mock_openhome_device(volume_max=100, cur_volume=50)
        await b.set_volume(40)
        assert calls["SetVolume"][0]["Value"] == 40
        assert await b.get_volume() == 50

    async def test_volume_scales_to_device_max(self):
        b = OpenHomeBackend(_make_renderer())
        b._device, calls = _mock_openhome_device(volume_max=80)
        await b.set_volume(50)
        assert calls["SetVolume"][0]["Value"] == 40  # 50% of 80

    async def test_mute_via_openhome(self):
        b = OpenHomeBackend(_make_renderer())
        b._device, calls = _mock_openhome_device(cur_mute=True)
        await b.set_mute(True)
        assert calls["SetMute"][0]["Value"] is True
        assert await b.get_mute() is True

    async def test_play_uri_is_rejected(self):
        b = OpenHomeBackend(_make_renderer())
        b._device, _ = _mock_openhome_device()
        with pytest.raises(RuntimeError, match="uses load_queue"):
            await b.play_uri("u", "t", "m")


class TestOpenHomeFactoryRouting:
    def test_openhome_is_default_for_openhome_renderers(self, monkeypatch):
        monkeypatch.delenv("RENFIELD_OPENHOME", raising=False)
        r = _make_renderer()
        r.is_openhome = True
        assert isinstance(queue_manager._make_backend(r), OpenHomeBackend)

    def test_opt_out_falls_back_to_avtransport(self, monkeypatch):
        monkeypatch.setenv("RENFIELD_OPENHOME", "0")
        r = _make_renderer()
        r.is_openhome = True
        assert isinstance(queue_manager._make_backend(r), AvTransportBackend)

    def test_non_openhome_renderer_uses_avtransport(self, monkeypatch):
        monkeypatch.delenv("RENFIELD_OPENHOME", raising=False)
        assert isinstance(queue_manager._make_backend(_make_renderer()), AvTransportBackend)


class TestQueueSessionOwnsQueue:
    async def test_start_hands_whole_queue_to_device(self):
        s = QueueSession(_make_renderer(), _make_tracks(3))
        s.backend = MagicMock()
        s.backend.owns_queue = True
        s.backend.connect = AsyncMock()
        s.backend.load_queue = AsyncMock()
        s.control_point.ensure_started = AsyncMock()
        s.control_point.factory = MagicMock()
        s.control_point.event_handler = MagicMock()
        await s.start()
        s.backend.load_queue.assert_awaited_once()
        items = s.backend.load_queue.call_args.args[0]
        assert len(items) == 3
        assert items[0][0] == s.tracks[0].url

    async def test_next_delegates_to_device_queue(self):
        s = QueueSession(_make_renderer(), _make_tracks(3))
        s.backend = MagicMock()
        s.backend.owns_queue = True
        s.backend.go_next = AsyncMock(return_value=True)
        track = await s.next()
        s.backend.go_next.assert_awaited_once()
        assert s.current_index == 1
        assert track is s.tracks[1]

    async def test_next_stops_at_device_queue_end(self):
        s = QueueSession(_make_renderer(), _make_tracks(2))
        s.current_index = 1
        s.backend = MagicMock()
        s.backend.owns_queue = True
        s.backend.go_next = AsyncMock(return_value=False)
        assert await s.next() is None


# ---------------------------------------------------------------------------
# T11: SonosBackend (wraps soco) — optional dep, env-gated, provisional
# ---------------------------------------------------------------------------

class TestSonosBackend:
    def test_owns_queue_and_supports_next(self):
        b = SonosBackend(_make_renderer())
        assert b.owns_queue is True
        assert b.supports_next is True

    def test_host_extracted_from_location(self):
        b = SonosBackend(_make_renderer())  # location http://192.168.1.100:49152/...
        assert b._host() == "192.168.1.100"

    async def test_load_queue_clears_adds_and_plays(self):
        b = SonosBackend(_make_renderer())
        b._soco = MagicMock()
        await b.load_queue([("u1", "A", "m"), ("u2", "B", "m")], start_index=0)
        b._soco.clear_queue.assert_called_once()
        assert b._soco.add_uri_to_queue.call_count == 2
        b._soco.play_from_queue.assert_called_once_with(0)
        assert b._queue_len == 2

    async def test_go_next_then_previous(self):
        b = SonosBackend(_make_renderer())
        b._soco = MagicMock()
        await b.load_queue([("u", "t", "m")] * 3)
        assert await b.go_next() is True
        assert b._current_index == 1
        b._soco.next.assert_called_once()
        assert await b.go_previous() is True
        assert await b.go_previous() is False

    async def test_volume_roundtrip(self):
        b = SonosBackend(_make_renderer())
        b._soco = MagicMock()
        await b.set_volume(40)
        assert b._soco.volume == 40
        assert await b.get_volume() == 40

    async def test_mute_roundtrip(self):
        b = SonosBackend(_make_renderer())
        b._soco = MagicMock()
        await b.set_mute(True)
        assert b._soco.mute is True
        assert await b.get_mute() is True

    async def test_play_uri_is_rejected(self):
        b = SonosBackend(_make_renderer())
        b._soco = MagicMock()
        with pytest.raises(RuntimeError, match="uses load_queue"):
            await b.play_uri("u", "t", "m")


class TestSonosFactoryRouting:
    def test_routes_to_sonos_when_env_enabled(self, monkeypatch):
        monkeypatch.setenv("RENFIELD_SONOS", "1")
        r = _make_renderer()
        r.is_sonos = True
        assert isinstance(queue_manager._make_backend(r), SonosBackend)

    def test_defaults_to_avtransport_without_env(self, monkeypatch):
        monkeypatch.delenv("RENFIELD_SONOS", raising=False)
        r = _make_renderer()
        r.is_sonos = True
        assert isinstance(queue_manager._make_backend(r), AvTransportBackend)


class TestSonosDetection:
    async def test_manufacturer_sonos_sets_flag(self):
        xml = _DESC_TMPL.format(name="Living Room", mfr="Sonos, Inc.",
                                model="One", extra_service="")
        r = await discovery._fetch_device_description(
            _desc_session(xml), "http://1.2.3.4:1400/desc.xml"
        )
        assert r is not None
        assert r.is_sonos is True


# ---------------------------------------------------------------------------
# T1 (partial): additive multi-interface M-SEARCH fan-out
# ---------------------------------------------------------------------------

class TestMultiInterfaceSearch:
    def test_local_ipv4_excludes_loopback_and_ipv6(self, monkeypatch):
        import sys
        import types

        class _IP:
            def __init__(self, ip):
                self.ip = ip

        class _Adapter:
            def __init__(self, ips):
                self.ips = ips

        fake = types.SimpleNamespace(get_adapters=lambda: [
            _Adapter([_IP("127.0.0.1"), _IP("10.0.0.5")]),
            _Adapter([_IP(("fe80::1", 0, 0))]),  # IPv6 tuple → skipped
        ])
        monkeypatch.setitem(sys.modules, "ifaddr", fake)
        assert discovery._local_ipv4_addresses() == ["10.0.0.5"]

    def test_local_ipv4_empty_when_ifaddr_unavailable(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "ifaddr", None)  # import → ImportError
        assert discovery._local_ipv4_addresses() == []

    async def test_search_fans_out_over_interfaces(self, monkeypatch):
        calls = []

        async def _fake_single(st, timeout, source_ip=None):
            calls.append(source_ip)
            return [f"http://{source_ip or 'default'}/d.xml"]

        monkeypatch.setattr(discovery, "_ssdp_search_single", _fake_single)
        monkeypatch.setattr(discovery, "_local_ipv4_addresses", lambda: ["10.0.0.5"])
        locs = await discovery._ssdp_search(timeout=0.01)
        # 2 search targets × (default-route + one interface)
        assert calls.count(None) == 2
        assert calls.count("10.0.0.5") == 2
        assert any("10.0.0.5" in loc for loc in locs)

    async def test_search_swallows_per_interface_failure(self, monkeypatch):
        async def _fake_single(st, timeout, source_ip=None):
            if source_ip == "10.0.0.5":
                raise OSError("interface vanished")
            return ["http://default/d.xml"]

        monkeypatch.setattr(discovery, "_ssdp_search_single", _fake_single)
        monkeypatch.setattr(discovery, "_local_ipv4_addresses", lambda: ["10.0.0.5"])
        # The default-route legs still succeed despite the interface leg raising.
        assert await discovery._ssdp_search(timeout=0.01) == ["http://default/d.xml"]


# ---------------------------------------------------------------------------
# OpenHome sibling-device discovery (validated against real Linn hardware)
# ---------------------------------------------------------------------------

class TestOpenHomeSiblingDiscovery:
    _OH_DESC = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
  <deviceType>urn:linn-co-uk:device:Source:1</deviceType>
  <friendlyName>Linn</friendlyName><UDN>uuid:oh</UDN>
  <serviceList>
    <service><serviceType>urn:av-openhome-org:service:Volume:4</serviceType>
      <controlURL>/v</controlURL></service>
    <service><serviceType>urn:av-openhome-org:service:Playlist:1</serviceType>
      <controlURL>/p</controlURL></service>
  </serviceList></device></root>"""

    async def test_fetch_openhome_location_detects_playlist(self):
        out = await discovery._fetch_openhome_location(
            _desc_session(self._OH_DESC), "http://10.0.0.9:55178/oh/device.xml"
        )
        assert out == ("10.0.0.9", "http://10.0.0.9:55178/oh/device.xml")

    async def test_non_openhome_device_returns_none(self):
        xml = _DESC_TMPL.format(name="Plain", mfr="X", model="Y", extra_service="")
        assert await discovery._fetch_openhome_location(
            _desc_session(xml), "http://10.0.0.9/d.xml"
        ) is None

    async def test_sibling_correlated_onto_renderer_by_host(self, monkeypatch):
        # The Linn topology: MediaRenderer + separate OpenHome Source device,
        # same host, different UDN. discover_renderers must flag is_openhome and
        # point openhome_location at the sibling.
        r = _make_renderer(name="Linn", udn="uuid:r")
        r.location = "http://10.0.0.9:55178/r/device.xml"
        r.is_openhome = False

        async def _fake_fetch(session, loc):
            return r if loc == "loc-r" else None

        async def _fake_oh(session, loc):
            if loc == "loc-oh":
                return ("10.0.0.9", "http://10.0.0.9:55178/oh/device.xml")
            return None

        monkeypatch.setattr(discovery, "_ssdp_search",
                            AsyncMock(return_value=["loc-r", "loc-oh"]))
        monkeypatch.setattr(discovery, "_fetch_device_description", _fake_fetch)
        monkeypatch.setattr(discovery, "_fetch_openhome_location", _fake_oh)
        discovery._renderer_cache = []
        discovery._cache_time = 0
        out = await discovery.discover_renderers(force=True)
        assert out[0].is_openhome is True
        assert out[0].openhome_location == "http://10.0.0.9:55178/oh/device.xml"
        discovery._renderer_cache = []
        discovery._cache_time = 0


class TestOpenHomeVersionFlexibleServices:
    async def test_volume_service_matched_by_prefix(self):
        # Real Linn exposes Volume:4, not Volume:1 — prefix match must find it.
        b = OpenHomeBackend(_make_renderer())
        _, calls = _mock_openhome_device()
        # Re-key the volume service under Volume:4 to mimic real hardware.
        dev = MagicMock()
        plain, calls = _mock_openhome_device(cur_volume=72)
        dev.services = {
            "urn:av-openhome-org:service:Playlist:1": plain.services[_OH_PLAYLIST],
            "urn:av-openhome-org:service:Volume:4": plain.services[_OH_VOLUME],
        }
        b._device = dev
        assert b._service("urn:av-openhome-org:service:Volume:") is not None
        assert await b.get_volume() == 72

    async def test_connect_uses_openhome_location(self, monkeypatch):
        r = _make_renderer()
        r.openhome_location = "http://10.0.0.9:55178/oh/device.xml"
        factory = MagicMock()
        factory.async_create_device = AsyncMock(return_value=MagicMock())
        b = OpenHomeBackend(r)
        await b.connect(lambda *a: None, factory=factory, event_handler=MagicMock())
        factory.async_create_device.assert_awaited_once_with(
            "http://10.0.0.9:55178/oh/device.xml"
        )


class TestOpenHomeRealTransportState:
    """OpenHomeBackend reads real device state from the Transport service
    (action TransportState → State), validated against a real Linn."""

    def _device_with_transport(self, state_value):
        async def _ts_call(**kw):
            return {"State": state_value}
        act = MagicMock()
        act.async_call = AsyncMock(side_effect=_ts_call)
        transport = MagicMock()
        transport.action.return_value = act
        dev = MagicMock()
        dev.services = {"urn:av-openhome-org:service:Transport:1": transport}
        return dev

    async def test_reads_and_maps_playing(self):
        b = OpenHomeBackend(_make_renderer())
        b._device = self._device_with_transport("Playing")
        assert await b.query_transport_state() == "PLAYING"
        assert b.transport_state == "PLAYING"

    async def test_maps_buffering_to_transitioning(self):
        b = OpenHomeBackend(_make_renderer())
        b._device = self._device_with_transport("Buffering")
        assert await b.query_transport_state() == "TRANSITIONING"

    async def test_maps_paused_and_stopped(self):
        b = OpenHomeBackend(_make_renderer())
        b._device = self._device_with_transport("Paused")
        assert await b.query_transport_state() == "PAUSED_PLAYBACK"
        b._device = self._device_with_transport("Stopped")
        assert await b.query_transport_state() == "STOPPED"

    async def test_falls_back_to_cache_without_transport_service(self):
        b = OpenHomeBackend(_make_renderer())
        b._device, _ = _mock_openhome_device()  # only Playlist + Volume
        b._transport_state = "PLAYING"
        assert await b.query_transport_state() == "PLAYING"


# ---------------------------------------------------------------------------
# SSDP listener (live cache) + session watchdog — ControlPoint background tasks
# ---------------------------------------------------------------------------

class TestSsdpListener:
    async def test_schedule_refresh_debounces_a_burst(self, monkeypatch):
        monkeypatch.setattr(cp_module, "_SSDP_REFRESH_DEBOUNCE", 0.05)
        cp = ControlPoint()
        calls = {"n": 0}

        async def on_change():
            calls["n"] += 1

        cp._on_change = on_change
        cp._schedule_refresh()
        cp._schedule_refresh()
        cp._schedule_refresh()  # burst — should coalesce
        await asyncio.sleep(0.15)
        assert calls["n"] == 1
        await cp.stop_background_tasks()

    async def test_callback_only_fires_on_relevant_sources(self, monkeypatch):
        from async_upnp_client.const import SsdpSource

        captured = {}

        class _FakeListener:
            def __init__(self, async_callback=None, **kw):
                captured["cb"] = async_callback

            async def async_start(self):
                pass

            async def async_stop(self):
                pass

        monkeypatch.setattr(
            "async_upnp_client.ssdp_listener.SsdpListener", _FakeListener
        )
        cp = ControlPoint()
        await cp.start_discovery_listener(AsyncMock())
        cb = captured["cb"]

        await cb(MagicMock(), "x", SsdpSource.ADVERTISEMENT_ALIVE)
        assert cp._refresh_task is not None  # relevant → scheduled
        cp._refresh_task.cancel()
        cp._refresh_task = None

        await cb(MagicMock(), "x", SsdpSource.SEARCH)
        assert cp._refresh_task is None  # plain search response → ignored
        await cp.stop_background_tasks()

    async def test_start_discovery_listener_idempotent(self, monkeypatch):
        class _FakeListener:
            def __init__(self, **kw):
                pass

            async def async_start(self):
                pass

            async def async_stop(self):
                pass

        monkeypatch.setattr(
            "async_upnp_client.ssdp_listener.SsdpListener", _FakeListener
        )
        cp = ControlPoint()
        await cp.start_discovery_listener(AsyncMock())
        first = cp._ssdp_listener
        await cp.start_discovery_listener(AsyncMock())
        assert cp._ssdp_listener is first  # not restarted
        await cp.stop_background_tasks()

    async def test_stop_background_tasks_stops_listener(self):
        cp = ControlPoint()
        listener = MagicMock()
        listener.async_stop = AsyncMock()
        cp._ssdp_listener = listener
        await cp.stop_background_tasks()
        listener.async_stop.assert_awaited_once()
        assert cp._ssdp_listener is None


class TestSessionWatchdog:
    async def test_watchdog_refreshes_sessions_read_only(self):
        cp = ControlPoint()
        sess = MagicMock()
        sess.refresh_state = AsyncMock()
        cp.register("u", sess)
        await cp.start_session_watchdog(interval=0.05)
        await asyncio.sleep(0.13)
        await cp.stop_background_tasks()
        assert sess.refresh_state.await_count >= 1
        # watchdog only ever calls refresh_state — never queue ops
        sess.next.assert_not_called()
        sess.stop.assert_not_called()

    async def test_watchdog_survives_a_session_refresh_error(self):
        cp = ControlPoint()
        bad = MagicMock()
        bad.refresh_state = AsyncMock(side_effect=RuntimeError("device gone"))
        good = MagicMock()
        good.refresh_state = AsyncMock()
        cp.register("bad", bad)
        cp.register("good", good)
        await cp.start_session_watchdog(interval=0.05)
        await asyncio.sleep(0.13)
        await cp.stop_background_tasks()
        # the bad session's error didn't stop the loop reaching the good one
        assert good.refresh_state.await_count >= 1

    async def test_watchdog_idempotent_start(self):
        cp = ControlPoint()
        await cp.start_session_watchdog(interval=10)
        first = cp._watchdog_task
        await cp.start_session_watchdog(interval=10)
        assert cp._watchdog_task is first
        await cp.stop_background_tasks()
