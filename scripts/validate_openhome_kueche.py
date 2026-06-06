"""End-to-end OpenHome playback validation against the Kitchen Linn (Küche).

Cross-generation check: the Küche is a 'Sneaky Music DS' (older) vs the only
previously-validated OpenHome unit ('Sneaky DSM', Ben's Zimmer). Drives the
real device through OpenHomeBackend via play_tracks:

  load_queue (Play track 1) → confirm real TransportState=PLAYING →
  next() (device Next) → confirm advanced + still PLAYING → stop → cleanup.

Plays AUDIBLE audio briefly. Leaves the device's volume setting untouched.
"""

import asyncio
import sys

from renfield_mcp_dlna import discovery, mediaserver, queue_manager
from renfield_mcp_dlna.queue_manager import Track

RENDERER = "küche"
SERVER = "jellyfin"
DWELL = 5.0  # seconds of audible playback per track


async def _find_album_tracks(server, factory, limit=2):
    """Walk Musik containers (browse only) until one holds >=2 playable tracks."""
    root = await mediaserver.browse(server, factory, "0", limit=20)
    musik = next((i for i in root["items"] if i["title"].lower().startswith("musik")), None)
    if musik is None:
        raise RuntimeError("no 'Musik' container on the server")

    # BFS through containers (depth-capped) for the first node with tracks.
    frontier = [musik["id"]]
    seen = set()
    for _ in range(400):  # hard cap on nodes visited
        if not frontier:
            break
        node = frontier.pop(0)
        if node in seen:
            continue
        seen.add(node)
        listing = await mediaserver.browse(server, factory, node, limit=30)
        tracks = [i for i in listing["items"] if i["playable"]]
        if len(tracks) >= 2:
            return tracks[:limit]
        frontier.extend(
            i["id"] for i in listing["items"] if i["type"] == "container"
        )
    raise RuntimeError("could not find an album with >=2 tracks")


async def main() -> None:
    renderers = await discovery.discover_renderers(force=True)
    r = next((x for x in renderers if RENDERER in x.name.lower()), None)
    if r is None:
        print(f"renderer {RENDERER!r} not found", file=sys.stderr)
        sys.exit(1)
    servers = await discovery.discover_servers(force=True)
    s = next((x for x in servers if SERVER in x.name.lower()), None)
    if s is None:
        print(f"server {SERVER!r} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Renderer: {r.name!r} [{r.manufacturer} {r.model_name}]")
    cp = queue_manager._default_control_point
    await cp.ensure_started()

    session = None
    try:
        playables = await _find_album_tracks(s, cp.factory, limit=2)
        tracks = [
            Track(url=p["url"], title=p.get("title", ""), artist=p.get("artist", ""),
                  album=p.get("album", ""))
            for p in playables
        ]
        print("Queue:")
        for i, t in enumerate(tracks, 1):
            print(f"  {i}. {t.title!r}  ({t.url[:70]}...)")

        print("\n=== load_queue → Play track 1 ===")
        session = await queue_manager.play_tracks(r, tracks, control_point=cp)
        await asyncio.sleep(DWELL)
        state1 = await session.backend.query_transport_state()
        print(f"  track index={session.current_index + 1}  real TransportState={state1}")
        assert state1 == "PLAYING", f"expected PLAYING, got {state1}"

        print("\n=== next() → device Next (track 2) ===")
        nxt = await session.next()
        print(f"  advanced to: {nxt.title if nxt else None!r}")
        await asyncio.sleep(DWELL)
        state2 = await session.backend.query_transport_state()
        print(f"  track index={session.current_index + 1}  real TransportState={state2}")
        assert session.current_index == 1, "index did not advance"
        assert state2 == "PLAYING", f"expected PLAYING after next, got {state2}"

        print("\n=== stop → cleanup ===")
        await session.stop()
        state3 = await session.backend.query_transport_state()
        print(f"  post-stop TransportState={state3}")
        print(f"  session in registry? {cp.get_session(r.udn) is not None}")
        assert cp.get_session(r.udn) is None, "session not cleaned up"

        print("\n✅ OpenHome playback VALIDATED on Sneaky Music DS (Küche).")
    finally:
        # The OpenHome device owns its queue and keeps playing on its own, so an
        # assertion/error that bails before the explicit stop above would leave
        # the speaker playing. Always stop (if still registered) + tear down.
        if session is not None and cp.get_session(r.udn) is not None:
            try:
                await session.stop()
            except Exception:
                pass
        await cp.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=90))
    except TimeoutError:
        print("Validation timed out after 90s", file=sys.stderr)
        sys.exit(1)
