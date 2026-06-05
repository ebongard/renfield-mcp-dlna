"""Per-device-family DIDL / protocolInfo strategy.

Why this exists (review Tension 1): async_upnp_client's
construct_play_media_metadata can't emit the DLNA.ORG_PN profile that strict TVs
(Samsung/LG/Sony) require, and it only echoes the source Content-Type. So
metadata is built here with a family-aware protocolInfo 4th-field.

Hybrid policy (keeps the safe behaviour for the renderers we already support):
  * AUDIO / standard renderers → protocolInfo 4th-field stays "*" unless the
    caller supplies a hint. Zero behaviour change for Linn/HiFiBerry/etc.
  * VIDEO → adds DLNA.ORG_OP/FLAGS (seek + streaming); TV families additionally
    get a DLNA.ORG_PN when we have a safe mapping.
  * Caller-supplied mime_type / dlna_features always win.

PROVISIONAL: the exact DLNA flag bitfield and PN mappings need validation
against real devices before they're trusted (see tasks/todo.md T8). The
*structure* — caller-hint precedence, family dispatch, memoisation seam — is the
durable part; the constants are conservative and easy to correct once a Samsung/
LG/Sony is on hand.
"""

from .didl import build_didl_metadata, build_video_didl_metadata

# DLNA 4th-field flags. OP=01 → server supports range requests (seek). FLAGS is
# the commonly-used "streaming + background-transfer + sender-paced" bitfield.
# PROVISIONAL values.
_DLNA_OP = "01"
_DLNA_FLAGS = "01700000000000000000000000000000"

_DEFAULT_AUDIO_MIME = "audio/flac"
_DEFAULT_VIDEO_MIME = "video/mp4"

# Manufacturers whose renderers are strict about DLNA video metadata.
_TV_MANUFACTURERS = ("samsung", "lg", "sony")


def _is_tv(renderer) -> bool:
    mfr = (getattr(renderer, "manufacturer", "") or "").lower()
    model = (getattr(renderer, "model_name", "") or "").lower()
    return any(t in mfr or t in model for t in _TV_MANUFACTURERS)


def _base_dlna_features() -> str:
    return f"DLNA.ORG_OP={_DLNA_OP};DLNA.ORG_FLAGS={_DLNA_FLAGS}"


def _video_profile_name(mime: str) -> str | None:
    """Best-effort DLNA.ORG_PN for a TV, or None when we shouldn't guess.

    PROVISIONAL and intentionally conservative: a *wrong* PN is worse than none,
    so this only returns a value for cases we're reasonably sure of, and returns
    None otherwise (falling back to OP/FLAGS only). Extend per real-device
    findings rather than guessing.
    """
    return None


def build_video_features(renderer) -> str:
    """The DLNA 4th-field for video on this renderer (PN for TVs + OP/FLAGS)."""
    base = _base_dlna_features()
    if _is_tv(renderer):
        pn = _video_profile_name(_DEFAULT_VIDEO_MIME)
        if pn:
            return f"DLNA.ORG_PN={pn};{base}"
    return base


def build(track, renderer=None) -> str:
    """Build DIDL-Lite metadata for `track` on `renderer`.

    `track` is duck-typed (url/title/artist/album/art_url/media_type and the
    optional mime_type/dlna_features hints).
    """
    media_type = getattr(track, "media_type", "audio")
    caller_mime = getattr(track, "mime_type", "") or ""
    caller_features = getattr(track, "dlna_features", "") or ""

    if media_type == "video":
        mime = caller_mime or _DEFAULT_VIDEO_MIME
        features = caller_features or build_video_features(renderer)
        return build_video_didl_metadata(
            track.url, getattr(track, "title", ""), mime_type=mime, dlna_features=features
        )

    # Audio / standard: preserve "*" (original behaviour) unless caller hints.
    mime = caller_mime or _DEFAULT_AUDIO_MIME
    features = caller_features or "*"
    return build_didl_metadata(
        track.url,
        getattr(track, "title", ""),
        getattr(track, "artist", ""),
        getattr(track, "album", ""),
        getattr(track, "art_url", ""),
        mime_type=mime,
        dlna_features=features,
    )
