"""Playback backends: per-device-family strategies for reaching a renderer.

A QueueSession owns the queue (track list + index) and reacts to transport
events; a PlaybackBackend owns *how* commands and volume reach a specific
device family. See base.PlaybackBackend.
"""

from .base import PlaybackBackend, TransportEvent
from .avtransport import AvTransportBackend
from .openhome import OpenHomeBackend
from .sonos import SonosBackend

__all__ = [
    "PlaybackBackend",
    "TransportEvent",
    "AvTransportBackend",
    "OpenHomeBackend",
    "SonosBackend",
]
