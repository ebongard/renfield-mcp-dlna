"""Live validation against real UPnP devices on the LAN (read-only).

Discovers renderers + servers, prints identity/capabilities, and (non-intrusively)
connects to each renderer to read volume/mute/capabilities — WITHOUT playing
audio. MediaServers get a root browse. No play_tracks here.
"""

import asyncio
import sys

from renfield_mcp_dlna import discovery, mediaserver
from renfield_mcp_dlna.backends.avtransport import AvTransportBackend
from renfield_mcp_dlna.control_point import ControlPoint


async def main() -> None:
    print("=== SSDP discovery (renderers) ===")
    renderers = await discovery.discover_renderers(force=True)
    if not renderers:
        print("  (no renderers found)")
    for r in renderers:
        flags = []
        if r.supports_next:
            flags.append("gapless")
        if r.is_openhome:
            flags.append("OpenHome")
        if r.is_sonos:
            flags.append("Sonos")
        print(f"  • {r.name!r} [{r.manufacturer} {r.model_name}] "
              f"{'/'.join(flags) or 'AVTransport'}")
        print(f"      udn={r.udn}")
        print(f"      location={r.location}")

    print("\n=== SSDP discovery (MediaServers) ===")
    servers = await discovery.discover_servers(force=True)
    if not servers:
        print("  (no servers found)")
    for s in servers:
        print(f"  • {s.name!r} [{s.manufacturer} {s.model_name}]  udn={s.udn}")

    # Non-intrusive renderer probe: connect + read volume/mute/caps, no playback.
    cp = ControlPoint()
    await cp.ensure_started()
    print("\n=== Renderer read-only probe (connect, no playback) ===")
    for r in renderers:
        backend = AvTransportBackend(r)
        try:
            await backend.connect(lambda *a: None, factory=cp.factory,
                                  event_handler=cp.event_handler)
            vol = await backend.get_volume()
            mute = await backend.get_mute()
            state = await backend.query_transport_state()
            modes = sorted(backend.valid_play_modes)
            print(f"  • {r.name!r}: volume={vol} mute={mute} state={state} "
                  f"play_modes={modes} caps={backend.capabilities}")
        except Exception as e:
            print(f"  • {r.name!r}: probe failed: {type(e).__name__}: {e}")
        finally:
            try:
                await backend.disconnect()
            except Exception:
                pass

    # MediaServer root browse (read-only).
    if servers:
        print("\n=== MediaServer root browse (first 5 items) ===")
        for s in servers:
            try:
                res = await mediaserver.browse(s, cp.factory, "0", limit=5)
                print(f"  • {s.name!r}: {res['total']} total; showing {res['returned']}:")
                for item in res["items"][:5]:
                    print(f"      [{item['type']}] {item['title']!r} id={item['id']}")
            except Exception as e:
                print(f"  • {s.name!r}: browse failed: {type(e).__name__}: {e}")

    await cp.aclose()
    print("\nDone.")


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=60))
    except TimeoutError:
        print("Validation timed out after 60s", file=sys.stderr)
        sys.exit(1)
