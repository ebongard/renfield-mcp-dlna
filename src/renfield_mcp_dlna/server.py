"""FastMCP server for DLNA media renderer control."""

import json
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

from . import discovery, queue_manager
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
    renderer = await discovery.find_renderer(renderer_name)
    if not renderer:
        return {"error": f"Renderer '{renderer_name}' not found"}

    try:
        track_list_raw = json.loads(tracks)
    except (json.JSONDecodeError, TypeError) as e:
        return {"error": f"Invalid tracks JSON: {e}"}

    if not isinstance(track_list_raw, list) or not track_list_raw:
        return {"error": "tracks must be a non-empty JSON array"}

    track_objects = []
    for i, t in enumerate(track_list_raw):
        if not isinstance(t, dict) or not t.get("url"):
            return {"error": f"Track {i} missing required 'url' field"}
        track_objects.append(
            Track(
                url=t["url"],
                title=t.get("title", ""),
                artist=t.get("artist", ""),
                album=t.get("album", ""),
                art_url=t.get("art_url", ""),
                media_type=t.get("media_type", "audio"),
            )
        )

    try:
        session = await queue_manager.play_tracks(renderer, track_objects)
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
        return {"error": f"Playback failed: {e}"}


@mcp.tool()
async def stop(renderer_name: str) -> dict:
    """Stop playback and clear queue on a DLNA renderer."""
    renderer = await discovery.find_renderer(renderer_name)
    if not renderer:
        return {"error": f"Renderer '{renderer_name}' not found"}

    session = queue_manager.get_session(renderer.udn)
    if not session:
        return {"error": f"No active playback on '{renderer.name}'"}

    await session.stop()
    return {"success": True, "renderer": renderer.name, "action": "stopped"}


@mcp.tool()
async def pause(renderer_name: str) -> dict:
    """Pause playback on a DLNA renderer."""
    renderer = await discovery.find_renderer(renderer_name)
    if not renderer:
        return {"error": f"Renderer '{renderer_name}' not found"}

    session = queue_manager.get_session(renderer.udn)
    if not session:
        return {"error": f"No active playback on '{renderer.name}'"}

    try:
        await session.pause()
        return {"success": True, "renderer": renderer.name, "action": "paused"}
    except Exception as e:
        return {"error": f"Pause failed: {e}"}


@mcp.tool()
async def resume(renderer_name: str) -> dict:
    """Resume playback on a DLNA renderer."""
    renderer = await discovery.find_renderer(renderer_name)
    if not renderer:
        return {"error": f"Renderer '{renderer_name}' not found"}

    session = queue_manager.get_session(renderer.udn)
    if not session:
        return {"error": f"No active playback on '{renderer.name}'"}

    try:
        await session.resume()
        return {"success": True, "renderer": renderer.name, "action": "resumed"}
    except Exception as e:
        return {"error": f"Resume failed: {e}"}


@mcp.tool()
async def next_track(renderer_name: str) -> dict:
    """Skip to the next track in the queue."""
    renderer = await discovery.find_renderer(renderer_name)
    if not renderer:
        return {"error": f"Renderer '{renderer_name}' not found"}

    session = queue_manager.get_session(renderer.udn)
    if not session:
        return {"error": f"No active playback on '{renderer.name}'"}

    track = await session.next()
    if track is None:
        return {"error": "Already at last track"}

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
    renderer = await discovery.find_renderer(renderer_name)
    if not renderer:
        return {"error": f"Renderer '{renderer_name}' not found"}

    session = queue_manager.get_session(renderer.udn)
    if not session:
        return {"error": f"No active playback on '{renderer.name}'"}

    track = await session.previous()
    if track is None:
        return {"error": "Already at first track"}

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
async def get_status(renderer_name: str) -> dict:
    """Get current playback status, track info, and queue position."""
    renderer = await discovery.find_renderer(renderer_name)
    if not renderer:
        return {"error": f"Renderer '{renderer_name}' not found"}

    session = queue_manager.get_session(renderer.udn)
    if not session:
        return {
            "renderer": renderer.name,
            "state": "idle",
            "message": "No active playback",
        }

    return session.status()


@mcp.tool()
async def set_volume(renderer_name: str, volume: int) -> dict:
    """Set playback volume (0-100) on a DLNA renderer."""
    renderer = await discovery.find_renderer(renderer_name)
    if not renderer:
        return {"error": f"Renderer '{renderer_name}' not found"}

    session = queue_manager.get_session(renderer.udn)
    if not session:
        return {"error": f"No active playback on '{renderer.name}'"}

    volume = max(0, min(100, volume))
    try:
        await session.set_volume(volume)
        return {"success": True, "renderer": renderer.name, "volume": volume}
    except Exception as e:
        return {"error": f"Failed to set volume: {e}"}


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
