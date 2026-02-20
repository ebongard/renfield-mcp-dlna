"""Tests for renfield-mcp-dlna MCP server."""

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
            assert "error" in result

    async def test_invalid_json(self):
        renderer = _make_renderer()
        with patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer):
            result = await server.play_tracks("HiFiBerry", "not json")
            assert "error" in result
            assert "Invalid tracks JSON" in result["error"]

    async def test_empty_tracks(self):
        renderer = _make_renderer()
        with patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer):
            result = await server.play_tracks("HiFiBerry", "[]")
            assert "error" in result
            assert "non-empty" in result["error"]

    async def test_track_missing_url(self):
        renderer = _make_renderer()
        with patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer):
            result = await server.play_tracks("HiFiBerry", '[{"title": "No URL"}]')
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
            assert "error" in result

    async def test_no_active_session(self):
        renderer = _make_renderer()
        with (
            patch.object(discovery, "find_renderer", new_callable=AsyncMock, return_value=renderer),
            patch.object(queue_manager, "get_session", return_value=None),
        ):
            result = await server.stop("HiFiBerry")
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


# ---------------------------------------------------------------------------
# QueueSession Unit Tests
# ---------------------------------------------------------------------------

class TestQueueSessionStatus:
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
