"""MediaServer (ContentDirectory) browsing via async_upnp_client's DmsDevice.

Turns this from a renderer-only controller into a UPnP AV *control point*: list
servers, browse/search their libraries, and resolve playable `res` URLs so the
LLM can pick a library item and play it on a renderer (play_from_server) without
the caller supplying URLs.

The DmsDevice is built from a discovered DlnaServer + the shared ControlPoint
factory. Browse/search are bounded (RequestedCount) so a huge library can't pull
everything in one call.
"""

import logging

from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.profiles.dlna import DmsDevice

from .discovery import DlnaServer

logger = logging.getLogger(__name__)

# Default cap on objects returned per browse/search (paginate for more).
DEFAULT_LIMIT = 200


async def _make_dms(server: DlnaServer, factory: UpnpFactory) -> DmsDevice:
    """Build a DmsDevice for a server. event_handler is None — ContentDirectory
    browse/search don't need an event subscription."""
    device = await factory.async_create_device(server.location)
    return DmsDevice(device, event_handler=None)


def _object_to_dict(obj) -> dict | None:
    """Map a didl_lite object to a plain dict. Returns None for Descriptors and
    anything without an id/class (which aren't browsable/playable)."""
    upnp_class = getattr(obj, "upnp_class", None)
    obj_id = getattr(obj, "id", None)
    if upnp_class is None or obj_id is None:
        return None

    is_container = str(upnp_class).startswith("object.container")
    url = ""
    resources = getattr(obj, "resources", None) or []
    for res in resources:
        if getattr(res, "uri", None):
            url = res.uri
            break

    media_type = "video" if "videoItem" in str(upnp_class) else "audio"
    return {
        "id": obj_id,
        "title": getattr(obj, "title", "") or "",
        "type": "container" if is_container else "item",
        "upnp_class": str(upnp_class),
        "url": url,
        "playable": bool(url) and not is_container,
        "media_type": media_type,
        "artist": getattr(obj, "artist", "") or getattr(obj, "creator", "") or "",
        "album": getattr(obj, "album", "") or "",
    }


def _parse_objects(didl_objects) -> list[dict]:
    return [d for obj in didl_objects if (d := _object_to_dict(obj)) is not None]


async def browse(
    server: DlnaServer,
    factory: UpnpFactory,
    object_id: str = "0",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """List the direct children of a container (object_id "0" is the root)."""
    dms = await _make_dms(server, factory)
    result = await dms.async_browse_direct_children(
        object_id, starting_index=offset, requested_count=limit
    )
    return {
        "server": server.name,
        "object_id": object_id,
        "items": _parse_objects(result.result),
        "returned": result.number_returned,
        "total": result.total_matches,
        "offset": offset,
    }


async def search(
    server: DlnaServer,
    factory: UpnpFactory,
    query: str,
    container_id: str = "0",
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Search the ContentDirectory for objects whose title contains `query`."""
    dms = await _make_dms(server, factory)
    if not dms.has_search_directory:
        raise RuntimeError(f"Server '{server.name}' does not support search")
    # Escape embedded quotes so the criteria string stays well-formed.
    safe = query.replace('"', '\\"')
    criteria = f'dc:title contains "{safe}"'
    result = await dms.async_search_directory(
        container_id, criteria, requested_count=limit
    )
    return {
        "server": server.name,
        "query": query,
        "items": _parse_objects(result.result),
        "returned": result.number_returned,
        "total": result.total_matches,
    }


async def resolve_playables(
    server: DlnaServer,
    factory: UpnpFactory,
    object_id: str,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """Resolve an object id to a list of playable items (each with a `url`).

    If the id is a container (album/playlist/folder), returns its playable
    direct children in order. If it's a single item, returns just that item.
    """
    dms = await _make_dms(server, factory)

    # Try as a container first (the common "play this album" case).
    children = await dms.async_browse_direct_children(object_id, requested_count=limit)
    playables = [d for d in _parse_objects(children.result) if d["playable"]]
    if playables:
        return playables

    # Otherwise treat it as a single object and read its own metadata.
    # async_browse_metadata returns ONE DidlObject (not a browse-result wrapper
    # with .result), so wrap it for _parse_objects.
    meta = await dms.async_browse_metadata(object_id)
    return [d for d in _parse_objects([meta]) if d["playable"]]
