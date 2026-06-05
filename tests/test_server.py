"""Tests for renfield-mcp-dlna MCP server."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from renfield_mcp_dlna import discovery, queue_manager, server
from renfield_mcp_dlna.didl import build_didl_metadata
from renfield_mcp_dlna.discovery import DlnaRenderer
from renfield_mcp_dlna.queue_manager import QueueSession, Track


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
        s._dmr = MagicMock()  # bound to a renderer
        s._transport_state = transport_state
        return s

    def test_stopped_when_unbound(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        assert s._dmr is None
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
        s._transport_state = "PLAYING"
        await s._confirm_playback_started("Track 1")  # no raise

    async def test_raises_when_stream_unreachable(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._transport_state = "NO_MEDIA_PRESENT"
        with pytest.raises(RuntimeError, match="did not start playback"):
            await s._confirm_playback_started("Track 1")

    async def test_raises_on_stopped(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._transport_state = "STOPPED"
        with pytest.raises(RuntimeError, match="did not start playback"):
            await s._confirm_playback_started("Track 1")

    async def test_no_false_fail_when_no_event(self, monkeypatch):
        # A renderer that simply hasn't emitted an event yet must not be failed.
        monkeypatch.setattr(queue_manager, "_PLAYBACK_CONFIRM_TIMEOUT", 0.0)
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._transport_state = None
        await s._confirm_playback_started("Track 1")  # warns, no raise

    async def test_confirms_via_poll_when_no_event(self):
        # Event-silent renderer (HiFiBerryOS): no LAST_CHANGE event ever fires,
        # but an active GetTransportInfo poll reports PLAYING → confirmed.
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._transport_state = None
        s._query_transport_state = AsyncMock(return_value="PLAYING")
        await s._confirm_playback_started("Track 1")  # no raise
        s._query_transport_state.assert_awaited()

    async def test_poll_detects_dead_renderer(self):
        # Poll reveals the renderer never started (404 stream → STOPPED).
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._transport_state = None
        s._query_transport_state = AsyncMock(return_value="STOPPED")
        with pytest.raises(RuntimeError, match="did not start playback"):
            await s._confirm_playback_started("Track 1")


class TestQueryTransportState:
    """_query_transport_state actively polls GetTransportInfo; refresh_state
    feeds that into the cached state so status() is accurate on event-silent
    renderers."""

    async def test_query_returns_normalized_state(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        dmr = MagicMock()
        dmr.async_update = AsyncMock()
        dmr.transport_state = MagicMock(value="PLAYING")
        s._dmr = dmr
        assert await s._query_transport_state() == "PLAYING"
        dmr.async_update.assert_awaited()

    async def test_query_normalizes_real_transport_state_enum(self):
        # The real DmrDevice yields a TransportState(str, Enum) member whose
        # str() is "TransportState.PLAYING" — only .value gives "PLAYING".
        # This guards against a regression to str(ts).upper().
        from async_upnp_client.profiles.dlna import TransportState

        s = QueueSession(_make_renderer(), _make_tracks(1))
        dmr = MagicMock()
        dmr.async_update = AsyncMock()
        dmr.transport_state = TransportState.PLAYING
        s._dmr = dmr
        assert await s._query_transport_state() == "PLAYING"

    async def test_query_bounded_by_timeout(self, monkeypatch):
        # A hung renderer must not block get_status: async_update is wrapped in
        # asyncio.wait_for. With no known transport_state, the poll returns None
        # within budget rather than waiting out the hang.
        monkeypatch.setattr(queue_manager, "_TRANSPORT_POLL_TIMEOUT", 0.01)

        async def _hang():
            await asyncio.sleep(1.0)

        s = QueueSession(_make_renderer(), _make_tracks(1))
        dmr = MagicMock()
        dmr.async_update = _hang
        dmr.transport_state = None
        s._dmr = dmr
        assert await s._query_transport_state() is None

    async def test_query_returns_none_when_unbound(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        assert s._dmr is None
        assert await s._query_transport_state() is None

    async def test_query_returns_none_on_error_without_known_state(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        dmr = MagicMock()
        dmr.async_update = AsyncMock(side_effect=RuntimeError("upnp timeout"))
        dmr.transport_state = None
        s._dmr = dmr
        assert await s._query_transport_state() is None

    async def test_query_falls_back_to_subscription_state_on_error(self):
        # Even if the active refresh raises, the lib keeps transport_state fresh
        # from the event subscription — report it instead of blanking to None.
        from async_upnp_client.profiles.dlna import TransportState

        s = QueueSession(_make_renderer(), _make_tracks(1))
        dmr = MagicMock()
        dmr.async_update = AsyncMock(side_effect=RuntimeError("upnp timeout"))
        dmr.transport_state = TransportState.PLAYING
        s._dmr = dmr
        assert await s._query_transport_state() == "PLAYING"

    async def test_refresh_state_updates_status(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._dmr = MagicMock()
        s._query_transport_state = AsyncMock(return_value="PLAYING")
        await s.refresh_state()
        assert s._transport_state == "PLAYING"
        assert s.status()["state"] == "playing"

    async def test_refresh_state_keeps_last_known_on_failed_poll(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._dmr = MagicMock()
        s._transport_state = "PLAYING"
        s._query_transport_state = AsyncMock(return_value=None)
        await s.refresh_state()
        assert s._transport_state == "PLAYING"  # untouched


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
        s._dmr = MagicMock()
        s._on_event(MagicMock(), [_SV("TransportState", "PLAYING"), _SV("TransportStatus", "OK")])
        assert s._transport_state == "PLAYING"
        assert s.status()["state"] == "playing"

    def test_dict_shape_tolerated(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._dmr = MagicMock()
        s._on_event(MagicMock(), {"TransportState": "PAUSED_PLAYBACK"})
        assert s._transport_state == "PAUSED_PLAYBACK"

    def test_empty_event_is_noop(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._on_event(MagicMock(), [])  # must not raise
        s._on_event(MagicMock(), None)
        assert s._transport_state is None

    def test_list_without_transport_state_leaves_state(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._dmr = MagicMock()
        s._transport_state = "PLAYING"
        s._on_event(MagicMock(), [_SV("Volume", 28), _SV("Mute", False)])
        assert s._transport_state == "PLAYING"  # untouched by unrelated vars


class TestVolumeCache:
    """get_volume reads a cached value (kept fresh by _on_event / set_volume)
    and only round-trips to the renderer via async_update when the cache is
    empty."""

    def test_on_event_caches_volume(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._dmr = MagicMock()
        s._on_event(MagicMock(), [_SV("Volume", 28)])
        assert s._volume == 28

    def test_on_event_bad_volume_ignored(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._dmr = MagicMock()
        s._on_event(MagicMock(), [_SV("Volume", "not-a-number")])
        assert s._volume is None

    async def test_set_volume_updates_cache(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._dmr = MagicMock()
        s._dmr.async_set_volume_level = AsyncMock()
        await s.set_volume(55)
        assert s._volume == 55
        s._dmr.async_set_volume_level.assert_awaited_once_with(0.55)

    async def test_get_volume_returns_cache_without_polling(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._dmr = MagicMock()
        s._dmr.async_update = AsyncMock()
        s._volume = 33
        vol = await s.get_volume()
        assert vol == 33
        s._dmr.async_update.assert_not_awaited()

    async def test_get_volume_falls_back_to_async_update(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._dmr = MagicMock()
        s._dmr.async_update = AsyncMock()
        s._dmr.volume_level = 0.4  # renderer reports 0.0-1.0
        assert s._volume is None
        vol = await s.get_volume()
        assert vol == 40
        s._dmr.async_update.assert_awaited_once()
        assert s._volume == 40  # now cached

    async def test_get_volume_none_when_cache_empty_and_level_none(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._dmr = MagicMock()
        s._dmr.async_update = AsyncMock()
        s._dmr.volume_level = None
        assert await s.get_volume() is None

    async def test_get_volume_none_when_no_dmr(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        assert s._dmr is None
        assert await s.get_volume() is None


class TestStopEventGuards:
    """Fixing _on_event activated the previously-dead STOPPED branches
    (auto-advance + queue-finished). A transient pre-playback STOPPED must not
    skip track 1 / tear down the session, and duplicate STOPPED events must not
    double-advance."""

    def _ev(self, state):
        return [_SV("TransportState", state)]

    async def test_transient_stop_before_play_does_not_advance_or_cleanup(self):
        s = QueueSession(_make_renderer(supports_next=False), _make_tracks(3))
        s._dmr = MagicMock()
        s._auto_advance = AsyncMock()
        s._cleanup = AsyncMock()
        s._on_event(MagicMock(), self._ev("STOPPED"))  # never reached PLAYING
        await asyncio.sleep(0)
        s._auto_advance.assert_not_awaited()
        s._cleanup.assert_not_awaited()
        assert s._advancing is False

    async def test_stop_after_play_advances_once(self):
        s = QueueSession(_make_renderer(supports_next=False), _make_tracks(3))
        s._dmr = MagicMock()
        s._auto_advance = AsyncMock()
        s._on_event(MagicMock(), self._ev("PLAYING"))
        s._on_event(MagicMock(), self._ev("STOPPED"))
        assert s._advancing is True  # set before scheduling
        await asyncio.sleep(0)
        s._auto_advance.assert_awaited_once()

    async def test_duplicate_stop_advances_only_once(self):
        s = QueueSession(_make_renderer(supports_next=False), _make_tracks(3))
        s._dmr = MagicMock()
        s._auto_advance = AsyncMock()
        s._on_event(MagicMock(), self._ev("PLAYING"))
        s._on_event(MagicMock(), self._ev("STOPPED"))
        s._on_event(MagicMock(), self._ev("STOPPED"))  # duplicate — guarded
        await asyncio.sleep(0)
        s._auto_advance.assert_awaited_once()

    async def test_transient_stop_single_track_does_not_cleanup(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._dmr = MagicMock()
        s._cleanup = AsyncMock()
        s._on_event(MagicMock(), self._ev("STOPPED"))  # never played
        await asyncio.sleep(0)
        s._cleanup.assert_not_awaited()

    async def test_stop_after_play_last_track_cleans_up(self):
        s = QueueSession(_make_renderer(), _make_tracks(1))
        s._dmr = MagicMock()
        s._cleanup = AsyncMock()
        s._on_event(MagicMock(), self._ev("PLAYING"))
        s._on_event(MagicMock(), self._ev("STOPPED"))
        await asyncio.sleep(0)
        s._cleanup.assert_awaited_once()
