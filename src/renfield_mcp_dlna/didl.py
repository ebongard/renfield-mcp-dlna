"""DIDL-Lite metadata generation for DLNA media renderers."""

from didl_lite import didl_lite


def build_didl_metadata(
    url: str,
    title: str = "",
    artist: str = "",
    album: str = "",
    art_url: str = "",
    mime_type: str = "audio/flac",
) -> str:
    """Return a DIDL-Lite XML string for a music track.

    The XML is used as metadata argument for SetAVTransportURI and
    SetNextAVTransportURI SOAP actions on DLNA renderers.
    """
    # Build protocol info string (required by UPnP AV)
    protocol_info = f"http-get:*:{mime_type}:*"

    # Create the resource (playback URL + protocol info)
    resource = didl_lite.Resource(
        uri=url,
        protocol_info=protocol_info,
    )

    # Build kwargs for MusicTrack constructor
    kwargs: dict = {
        "id": "0",
        "parent_id": "-1",
        "title": title or "Unknown",
        "restricted": "1",
        "resources": [resource],
    }
    if artist:
        kwargs["creator"] = artist
    if album:
        kwargs["album"] = album
    if art_url:
        kwargs["album_art_uri"] = art_url

    item = didl_lite.MusicTrack(**kwargs)
    return didl_lite.to_xml_string(item).decode("utf-8")
