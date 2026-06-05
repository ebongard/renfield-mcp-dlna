"""DIDL-Lite metadata generation for DLNA media renderers."""

from didl_lite import didl_lite


def build_didl_metadata(
    url: str,
    title: str = "",
    artist: str = "",
    album: str = "",
    art_url: str = "",
    mime_type: str = "audio/flac",
    dlna_features: str = "*",
) -> str:
    """Return a DIDL-Lite XML string for a music track.

    The XML is used as metadata argument for SetAVTransportURI and
    SetNextAVTransportURI SOAP actions on DLNA renderers.

    `dlna_features` is the 4th protocolInfo field (DLNA.ORG_OP/FLAGS/PN). The
    metadata strategy supplies it; "*" means unspecified (the original default).
    """
    # Build protocol info string (required by UPnP AV)
    protocol_info = f"http-get:*:{mime_type}:{dlna_features}"

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


def build_video_didl_metadata(
    url: str,
    title: str = "",
    mime_type: str = "video/mp4",
    dlna_features: str = "*",
) -> str:
    """Return a DIDL-Lite XML string for a video item (Movie).

    Used for SetAVTransportURI when playing video content on DLNA
    renderers (Smart TVs). `dlna_features` carries DLNA.ORG_PN/OP/FLAGS that
    strict TVs require (supplied by the metadata strategy).
    """
    protocol_info = f"http-get:*:{mime_type}:{dlna_features}"
    resource = didl_lite.Resource(uri=url, protocol_info=protocol_info)

    item = didl_lite.Movie(
        id="0",
        parent_id="-1",
        title=title or "Unknown",
        restricted="1",
        resources=[resource],
    )
    return didl_lite.to_xml_string(item).decode("utf-8")
