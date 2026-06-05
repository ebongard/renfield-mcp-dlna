"""The PlaybackBackend abstraction.

Split of responsibility (see tasks/todo.md, Issue 1):

    QueueSession                         PlaybackBackend (this ABC)
    ────────────                         ──────────────────────────
    owns the queue (tracks + index)      owns the device connection (_dmr)
    reacts to transport events:          parses raw LAST_CHANGE events,
      gapless transition, auto-advance,    caches transport_state + volume,
      queue-finished cleanup               forwards (state, uri) to the session
    builds DIDL metadata per track        executes play/preload/pause/stop
                                          does volume/mute (device-correct scaling)

Why an interface at all: the three device families we support reach the device
in fundamentally different ways. AVTransport (the default impl, AvTransportBackend)
keeps the queue CLIENT-side — we push one CurrentURI and preload the next via
SetNextAVTransportURI. OpenHome renderers (Linn) keep the queue on the DEVICE
via the av-openhome-org Playlist service. Sonos has its own queue + zone model.

This ABC is deliberately small and PROVISIONAL: it is finalized only after the
OpenHome spike proves the "device owns the queue" shape (Tension 2), so it is
not frozen around AVTransport-only assumptions. `owns_queue` is the seam that
flags that difference to QueueSession.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a runtime dependency in the abstract layer
    from async_upnp_client.client_factory import UpnpFactory
    from async_upnp_client.event_handler import UpnpEventHandler

# A parsed transport event forwarded from a backend to its session:
# (transport_state, current_track_uri). Either field may be None when the
# event didn't carry it. transport_state values are the raw UPnP strings
# ("PLAYING", "STOPPED", ...) so both sides share one vocabulary.
TransportEvent = Callable[[str | None, str | None], None]

# UPnP AVTransport TransportState buckets, shared by backends and the session.
# A renderer that actually started playback reaches an OK state; one that
# couldn't fetch/decode the stream stays in a DEAD state.
TRANSPORT_OK = frozenset({"PLAYING", "PAUSED_PLAYBACK"})
TRANSPORT_DEAD = frozenset({"STOPPED", "NO_MEDIA_PRESENT"})


class PlaybackBackend(ABC):
    """How playback commands and volume reach one renderer.

    Implementations cache the device's last-known transport state (from the
    event subscription and/or active polling) and expose it via
    `transport_state` so the session never has to assume "playing".
    """

    # True when the *device* owns the play queue (OpenHome). For AVTransport
    # we own the queue, so this is False and the session drives next/preload.
    owns_queue: bool = False

    @property
    @abstractmethod
    def supports_next(self) -> bool:
        """Whether the device can preload a next track for gapless transition."""

    @property
    @abstractmethod
    def connected(self) -> bool:
        """Whether the backend is currently bound to a device."""

    @property
    @abstractmethod
    def transport_state(self) -> str | None:
        """Last-known raw UPnP TransportState, or None if never reported."""

    @abstractmethod
    async def connect(
        self,
        on_event: TransportEvent,
        *,
        factory: "UpnpFactory",
        event_handler: "UpnpEventHandler",
    ) -> None:
        """Bind to the device and subscribe to events, forwarding parsed
        (transport_state, current_uri) changes to `on_event`.

        `factory`/`event_handler` are the shared UPnP infrastructure, injected
        rather than reached for globally. This is PROVISIONAL: it suits the
        UPnP-based backends (AVTransport, OpenHome both use async_upnp_client),
        but the Sonos/soco backend won't need them — the signature is revisited
        once the ControlPoint (which will own this infra) lands. Until then,
        keeping them in the contract stops a new backend silently diverging.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Stop and unsubscribe; best-effort, never raises."""

    @abstractmethod
    async def play_uri(self, url: str, title: str, metadata: str) -> None:
        """Set the current transport URI to `url` and start playing it now."""

    @abstractmethod
    async def preload_next(self, url: str, title: str, metadata: str) -> None:
        """Preload `url` as the next track (SetNextAVTransportURI) for gapless
        transition. No-op semantics are the impl's choice when unsupported."""

    @abstractmethod
    async def play(self) -> None:
        """Resume/start playback of the current transport."""

    @abstractmethod
    async def pause(self) -> None:
        """Pause playback."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop playback (without tearing down the subscription)."""

    @abstractmethod
    async def query_transport_state(self) -> str | None:
        """Actively poll the device for its current TransportState (for
        renderers that don't emit LAST_CHANGE events). Best-effort: returns
        None on failure rather than raising."""

    @abstractmethod
    async def refresh(self) -> None:
        """Poll and update the cached transport_state, leaving the last-known
        value untouched if the poll fails."""

    @abstractmethod
    async def set_volume(self, volume: int) -> None:
        """Set volume as a 0-100 percentage (impl handles device scaling)."""

    @abstractmethod
    async def get_volume(self) -> int | None:
        """Current volume as 0-100, or None if the device can't report it."""

    @abstractmethod
    async def set_mute(self, mute: bool) -> None:
        """Mute (True) or unmute (False)."""
