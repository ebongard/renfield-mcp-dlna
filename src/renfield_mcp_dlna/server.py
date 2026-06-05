"""FastMCP server for DLNA media renderer control."""

import json
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

from . import discovery, mediaserver, queue_manager
from .queue_manager import Track

# Logging to stderr (stdout is reserved for MCP stdio protocol)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "renfield-dlna",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "9091")),
)


# ---------------------------------------------------------------------------
# Tool plumbing
#
# Every tool resolved a renderer (and usually its session) and hand-built the
# same {"success": False, "error": ...} guards. That boilerplate was copied
# across all tools and grows with every new one, so the lookups live here once.
# resolve_* raise ToolError; _error() formats the single error-dict shape.
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """Raised inside a tool to short-circuit with a structured error response."""


def _error(message: str) -> dict:
    """The one place the failure-response shape is defined."""
    return {"success": False, "error": message}


async def _resolve_renderer(renderer_name: str):
    """Find a renderer by (partial) name or raise ToolError. Patchable via
    discovery.find_renderer (kept as the indirection point tests mock)."""
    renderer = await discovery.find_renderer(renderer_name)
    if not renderer:
        raise ToolError(f"Renderer '{renderer_name}' not found")
    return renderer


async def _resolve_session(renderer_name: str):
    """Resolve (renderer, active session) or raise ToolError if either is
    missing. Used by every tool that acts on in-progress playback."""
    renderer = await _resolve_renderer(renderer_name)
    session = queue_manager.get_session(renderer.udn)
    if not session:
        raise ToolError(f"No active playback on '{renderer.name}'")
    return renderer, session


async def _resolve_server(server_name: str):
    """Find a MediaServer by (partial) name or raise ToolError."""
    server = await discovery.find_server(server_name)
    if not server:
        raise ToolError(f"Server '{server_name}' not found")
    return server


async def _content_directory_factory():
    """The shared UPnP factory (used to build DmsDevices), ensuring the control
    point's infra is started first."""
    cp = queue_manager._default_control_point
    await cp.ensure_started()
    return cp.factory


@mcp.tool()
async def list_renderers(force_refresh: bool = False) -> dict:
    """Discover DLNA media renderers on the network.

    Returns name and supports_queue flag for each renderer.

    Args:
        force_refresh: If true, bypass the 5-minute cache and rescan the network.
    """
    renderers = await discovery.discover_renderers(force=force_refresh)
    items = [
        {
            "name": r.name,
            "supports_queue": r.supports_next,
        }
        for r in renderers
    ]
    return {"total": len(items), "renderers": items}


@mcp.tool()
async def play_tracks(
    renderer_name: str,
    tracks: str,
) -> dict:
    """Play a list of tracks on a DLNA renderer with gapless queue.

    First track starts immediately. Subsequent tracks are preloaded for
    gapless transition on supported renderers.

    Args:
        renderer_name: Name (or partial name) of the DLNA renderer.
        tracks: JSON array of track objects, each with:
            url (required), title, artist, album, art_url.
    """
    try:
        renderer = await _resolve_renderer(renderer_name)
    except ToolError as e:
        return _error(str(e))

    try:
        track_list_raw = json.loads(tracks)
    except (json.JSONDecodeError, TypeError) as e:
        return _error(f"Invalid tracks JSON: {e}")

    if not isinstance(track_list_raw, list) or not track_list_raw:
        return _error("tracks must be a non-empty JSON array")

    track_objects = []
    for i, t in enumerate(track_list_raw):
        if not isinstance(t, dict) or not t.get("url"):
            return _error(f"Track {i} missing required 'url' field")
        track_objects.append(
            Track(
                url=t["url"],
                title=t.get("title", ""),
                artist=t.get("artist", ""),
                album=t.get("album", ""),
                art_url=t.get("art_url", ""),
                media_type=t.get("media_type", "audio"),
                mime_type=t.get("mime_type", ""),
                dlna_features=t.get("dlna_features", ""),
            )
        )

    try:
        await queue_manager.play_tracks(renderer, track_objects)
        return {
            "success": True,
            "renderer": renderer.name,
            "total_tracks": len(track_objects),
            "supports_gapless": renderer.supports_next,
            "now_playing": {
                "track": 1,
                "title": track_objects[0].title,
                "artist": track_objects[0].artist,
                "album": track_objects[0].album,
            },
        }
    except Exception as e:
        logger.error(f"play_tracks failed on {renderer.name}: {e}", exc_info=True)
        return _error(f"Playback failed: {e}")


@mcp.tool()
async def stop(renderer_name: str) -> dict:
    """Stop playback and clear queue on a DLNA renderer."""
    try:
        renderer, session = await _resolve_session(renderer_name)
    except ToolError as e:
        return _error(str(e))

    await session.stop()
    return {"success": True, "renderer": renderer.name, "action": "stopped"}


@mcp.tool()
async def pause(renderer_name: str) -> dict:
    """Pause playback on a DLNA renderer."""
    try:
        renderer, session = await _resolve_session(renderer_name)
    except ToolError as e:
        return _error(str(e))

    try:
        await session.pause()
        return {"success": True, "renderer": renderer.name, "action": "paused"}
    except Exception as e:
        return _error(f"Pause failed: {e}")


@mcp.tool()
async def resume(renderer_name: str) -> dict:
    """Resume playback on a DLNA renderer."""
    try:
        renderer, session = await _resolve_session(renderer_name)
    except ToolError as e:
        return _error(str(e))

    try:
        await session.resume()
        return {"success": True, "renderer": renderer.name, "action": "resumed"}
    except Exception as e:
        return _error(f"Resume failed: {e}")


@mcp.tool()
async def next_track(renderer_name: str) -> dict:
    """Skip to the next track in the queue."""
    try:
        renderer, session = await _resolve_session(renderer_name)
    except ToolError as e:
        return _error(str(e))

    track = await session.next()
    if track is None:
        return _error("Already at last track")

    return {
        "success": True,
        "renderer": renderer.name,
        "now_playing": {
            "track": session.current_index + 1,
            "total_tracks": len(session.tracks),
            "title": track.title,
            "artist": track.artist,
        },
    }


@mcp.tool()
async def previous_track(renderer_name: str) -> dict:
    """Go to the previous track in the queue."""
    try:
        renderer, session = await _resolve_session(renderer_name)
    except ToolError as e:
        return _error(str(e))

    track = await session.previous()
    if track is None:
        return _error("Already at first track")

    return {
        "success": True,
        "renderer": renderer.name,
        "now_playing": {
            "track": session.current_index + 1,
            "total_tracks": len(session.tracks),
            "title": track.title,
            "artist": track.artist,
        },
    }


@mcp.tool()
async def seek(renderer_name: str, position_seconds: int) -> dict:
    """Seek to a position within the current track.

    Args:
        renderer_name: Name (or partial name) of the DLNA renderer.
        position_seconds: Target offset in seconds from the start of the track.
    """
    try:
        renderer, session = await _resolve_session(renderer_name)
    except ToolError as e:
        return _error(str(e))

    try:
        await session.seek(position_seconds)
        return {
            "success": True,
            "renderer": renderer.name,
            "position": max(0, position_seconds),
        }
    except Exception as e:
        return _error(f"Seek failed: {e}")


@mcp.tool()
async def set_play_mode(renderer_name: str, mode: str) -> dict:
    """Set the play mode on a DLNA renderer.

    UPnP exposes a single play mode (not independent repeat + shuffle toggles),
    so setting one replaces the other.

    Args:
        renderer_name: Name (or partial name) of the DLNA renderer.
        mode: One of normal, repeat_one, repeat_all, shuffle, random. The
            renderer's accepted modes are reported as `valid_play_modes` in
            get_status.
    """
    try:
        renderer, session = await _resolve_session(renderer_name)
    except ToolError as e:
        return _error(str(e))

    try:
        await session.set_play_mode(mode)
        return {"success": True, "renderer": renderer.name, "play_mode": mode.strip().lower()}
    except Exception as e:
        return _error(f"Failed to set play mode: {e}")


@mcp.tool()
async def get_status(renderer_name: str) -> dict:
    """Get current playback status, track info, and queue position."""
    try:
        renderer = await _resolve_renderer(renderer_name)
    except ToolError as e:
        return _error(str(e))

    # No session is not an error here — an idle renderer is a valid status.
    session = queue_manager.get_session(renderer.udn)
    if not session:
        return {
            "renderer": renderer.name,
            "state": "idle",
            "message": "No active playback",
        }

    # Actively poll the renderer (GetTransportInfo) so the reported state is
    # accurate even for renderers that don't emit LAST_CHANGE events.
    await session.refresh_state()
    status = session.status()
    # Volume/mute need async RC reads, so they're added here rather than in the
    # sync status() snapshot.
    status["volume"] = await session.get_volume()
    status["muted"] = await session.get_mute()
    return status


@mcp.tool()
async def set_volume(renderer_name: str, volume: int) -> dict:
    """Set playback volume (0-100) on a DLNA renderer."""
    try:
        renderer, session = await _resolve_session(renderer_name)
    except ToolError as e:
        return _error(str(e))

    volume = max(0, min(100, volume))
    try:
        await session.set_volume(volume)
        return {"success": True, "renderer": renderer.name, "volume": volume}
    except Exception as e:
        return _error(f"Failed to set volume: {e}")


@mcp.tool()
async def get_volume(renderer_name: str) -> dict:
    """Get current playback volume (0-100) on a DLNA renderer.

    Returns volume=None if the renderer cannot report it.
    """
    try:
        renderer, session = await _resolve_session(renderer_name)
    except ToolError as e:
        return _error(str(e))

    volume = await session.get_volume()
    return {"success": True, "renderer": renderer.name, "volume": volume}


@mcp.tool()
async def get_mute(renderer_name: str) -> dict:
    """Get current mute state on a DLNA renderer.

    Returns muted=None if the renderer cannot report it.
    """
    try:
        renderer, session = await _resolve_session(renderer_name)
    except ToolError as e:
        return _error(str(e))

    muted = await session.get_mute()
    return {"success": True, "renderer": renderer.name, "muted": muted}


@mcp.tool()
async def set_mute(renderer_name: str, mute: bool) -> dict:
    """Mute (mute=true) or unmute (mute=false) a DLNA renderer.

    Uses native RenderingControl SetMute — the renderer restores the prior
    volume on unmute, so no volume value needs to be stored.
    """
    try:
        renderer, session = await _resolve_session(renderer_name)
    except ToolError as e:
        return _error(str(e))

    try:
        await session.set_mute(mute)
        return {"success": True, "renderer": renderer.name, "muted": mute}
    except Exception as e:
        return _error(f"Failed to set mute: {e}")


# ---------------------------------------------------------------------------
# MediaServer (ContentDirectory) tools — browse a library and play from it
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_servers(force_refresh: bool = False) -> dict:
    """Discover DLNA MediaServers (content libraries) on the network.

    Args:
        force_refresh: If true, bypass the 5-minute cache and rescan.
    """
    servers = await discovery.discover_servers(force=force_refresh)
    items = [
        {"name": s.name, "manufacturer": s.manufacturer, "model": s.model_name}
        for s in servers
    ]
    return {"total": len(items), "servers": items}


@mcp.tool()
async def browse_server(
    server_name: str, object_id: str = "0", limit: int = 200, offset: int = 0
) -> dict:
    """Browse a MediaServer's library (direct children of a container).

    Args:
        server_name: Name (or partial name) of the MediaServer.
        object_id: Container to list ("0" is the root). Use ids from a prior
            browse/search to drill into folders/albums.
        limit: Max items to return (pagination cap).
        offset: Starting index for pagination.
    """
    try:
        server = await _resolve_server(server_name)
    except ToolError as e:
        return _error(str(e))
    try:
        factory = await _content_directory_factory()
        result = await mediaserver.browse(server, factory, object_id, limit, offset)
        return {"success": True, **result}
    except Exception as e:
        return _error(f"Browse failed: {e}")


@mcp.tool()
async def search_server(server_name: str, query: str, limit: int = 200) -> dict:
    """Search a MediaServer's library by title.

    Args:
        server_name: Name (or partial name) of the MediaServer.
        query: Text to match against item titles.
        limit: Max items to return.
    """
    try:
        server = await _resolve_server(server_name)
    except ToolError as e:
        return _error(str(e))
    try:
        factory = await _content_directory_factory()
        result = await mediaserver.search(server, factory, query, limit=limit)
        return {"success": True, **result}
    except Exception as e:
        return _error(f"Search failed: {e}")


@mcp.tool()
async def play_from_server(
    server_name: str, object_id: str, renderer_name: str
) -> dict:
    """Play a library object (album/playlist/folder/track) on a renderer.

    Resolves the object's playable items on the MediaServer, then plays them as
    a gapless queue on the renderer — no content URLs needed from the caller.

    Args:
        server_name: Name (or partial name) of the MediaServer holding the item.
        object_id: The container or item id to play (from browse/search).
        renderer_name: Name (or partial name) of the DLNA renderer to play on.
    """
    try:
        server = await _resolve_server(server_name)
        renderer = await _resolve_renderer(renderer_name)
    except ToolError as e:
        return _error(str(e))

    try:
        factory = await _content_directory_factory()
        playables = await mediaserver.resolve_playables(server, factory, object_id)
    except Exception as e:
        return _error(f"Could not resolve items from server: {e}")

    if not playables:
        return _error(f"No playable items found for object '{object_id}'")

    tracks = [
        Track(
            url=p["url"],
            title=p["title"],
            artist=p["artist"],
            album=p["album"],
            media_type=p["media_type"],
        )
        for p in playables
    ]

    try:
        await queue_manager.play_tracks(renderer, tracks)
    except Exception as e:
        logger.error(f"play_from_server failed on {renderer.name}: {e}", exc_info=True)
        return _error(f"Playback failed: {e}")

    return {
        "success": True,
        "server": server.name,
        "renderer": renderer.name,
        "total_tracks": len(tracks),
        "now_playing": {
            "track": 1,
            "title": tracks[0].title,
            "artist": tracks[0].artist,
            "album": tracks[0].album,
        },
    }


def main():
    """Entry point for console script and python -m.

    Transport is selected via MCP_TRANSPORT env var:
      - "stdio" (default): MCP stdio protocol over stdin/stdout
      - "streamable-http": HTTP server for remote connections

    For streamable-http, set MCP_PORT (default: 9091) and MCP_HOST (default: 0.0.0.0).
    """
    transport = os.getenv("MCP_TRANSPORT", "stdio")

    if transport == "streamable-http":
        logger.info(
            f"Starting DLNA MCP server on {mcp.settings.host}:{mcp.settings.port} (streamable-http)"
        )
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
