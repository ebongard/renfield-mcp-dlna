# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP server that discovers DLNA media renderers via SSDP and controls them
directly over UPnP AVTransport — there is no intermediate media server in the
control path. Media content URLs (passed into `play_tracks`) point at an
external server (Jellyfin's built-in DLNA server in the deployed setup); this
server only orchestrates playback. It runs as a subprocess (stdio) for an MCP
client, or as a standalone host service (streamable-http) for the Renfield
backend running in Docker.

## Commands

```bash
pip install -e ".[dev]"          # editable install with test deps
python -m pytest                 # run all tests (asyncio_mode=auto, no markers needed)
python -m pytest tests/test_server.py::test_name   # run a single test
ruff check src tests             # lint (ruff is used; see .ruff_cache)

renfield-mcp-dlna                # run server, stdio transport (default)
MCP_TRANSPORT=streamable-http MCP_PORT=9091 renfield-mcp-dlna   # run as HTTP service on :9091/mcp
```

Requires Python >= 3.11.

## Architecture

> Now a full UPnP control point: renderers + MediaServers, per-device-class
> backends (AVTransport / OpenHome / Sonos), live SSDP-listener discovery, and a
> session watchdog — built and (mostly) hardware-validated. Remaining items and
> validation status are in `tasks/todo.md`; read it before extending.

Source modules under `src/renfield_mcp_dlna/`, layered:

- **`server.py`** — thin FastMCP tool layer. Each `@mcp.tool()` resolves a
  renderer/session via the `resolve_renderer`/`resolve_session` helpers (which
  raise `ToolError`), delegates to `queue_manager`, and returns a
  `{"success": bool, ...}` dict via `_error()`. Tools never raise to the client.
  `main()` selects transport from `MCP_TRANSPORT`.
- **`discovery.py`** — SSDP M-SEARCH (raw UDP multicast to
  `239.255.255.250:1900`) + device-description XML parsing. Captures identity
  (`manufacturer`/`model_name`) and `is_openhome` (av-openhome-org Playlist
  present) for backend selection. 5-min module cache; `find_renderer()` matches
  case-insensitive substring on friendly name.
- **`control_point.py`** — `ControlPoint` owns the shared UPnP infra (requester
  / notify server / event handler / factory) and the per-UDN session registry.
  `ensure_started()` closes the lazy-init race with a double-checked lock;
  `unregister()` tears the infra down when the last session leaves. Also owns the
  background tasks (streamable-http only, started in `server.main`): a passive
  **SSDP listener** (`start_discovery_listener` → debounced discovery refresh on
  alive/byebye, so the cache stays live) and a read-only **session watchdog**
  (`start_session_watchdog` → periodic `refresh_state`; never touches the queue —
  GENA renewal stays with the library's `auto_resubscribe`). `stop_background_tasks`
  cancels both on shutdown.
- **`backends/`** — `PlaybackBackend` ABC (`base.py`) + three impls. The backend
  owns **all device I/O**. `AvTransportBackend` (default, client-owned queue:
  `DmrDevice`, RenderingControl volume/mute, polling, raw `LAST_CHANGE` parsing).
  `OpenHomeBackend` and `SonosBackend` are **`owns_queue=True`** (device holds the
  queue): they implement `load_queue`/`go_next`/`go_previous` and QueueSession
  hands the whole queue over once. `OpenHomeBackend` is **hardware-validated**
  (Linn — discovery via the sibling `Source` device, `Volume:4`, playback to real
  `TransportState=PLAYING`) and is the **default** for OpenHome renderers
  (`RENFIELD_OPENHOME=0` opts out to AVTransport). `SonosBackend` is still
  **PROVISIONAL** (mock+spec only), opt-in via `RENFIELD_SONOS=1`; `soco` is an
  optional dep (`pip install '.[sonos]'`).
- **`metadata.py`** — device-family DIDL/protocolInfo strategy. Audio keeps the
  `*` 4th-field (no regression); video adds DLNA.ORG_OP/FLAGS, TV families
  (Samsung/LG/Sony) get a `DLNA.ORG_PN` seam. Caller `mime_type`/`dlna_features`
  hints win. **PROVISIONAL** flag values (need real-TV validation). `QueueSession`
  memoises built metadata per URL.
- **`mediaserver.py`** — `DmsDevice`-backed ContentDirectory browse/search +
  `resolve_playables` (container → children, item → metadata). Powers the
  `list_servers`/`browse_server`/`search_server`/`play_from_server` tools.
- **`queue_manager.py`** — `QueueSession` owns the queue + the gapless/auto-
  advance event *reaction* (client-owned-queue backends) or delegates to the
  device (`owns_queue` backends), via its `backend`. `_make_backend()` is the
  factory (selects by identity + env). A module `_default_control_point` backs
  the `play_tracks`/`get_session` facade.

### Key behaviors that span files

- **Gapless vs. auto-advance.** Renderers advertising `SetNextAVTransportURI`
  (`supports_next`, from the AVTransport SCPD at discovery) get the next track
  preloaded. Renderers without it are auto-advanced in
  `QueueSession._on_transport_event` when a `STOPPED` arrives *after* the prior
  reported state was OK (the `played` gate, computed from `_prev_transport_state`
  — a per-event mirror, **not** a sticky flag, so a `TRANSITIONING` between
  `PLAYING` and a transient `STOPPED` isn't read as track-end). `_advancing`
  dedupes duplicate `STOPPED`.

- **Event flow is split.** The backend's `_handle_raw_event` parses the
  `LAST_CHANGE` event — delivery is a **list** of state-variable objects (not a
  dict); folding it wrong silently breaks all transition detection — caches
  transport-state + volume, then forwards `(transport_state, current_uri)` to
  `QueueSession._on_transport_event`. `status()` reports the backend's real
  transport state, never "playing" just because a backend is bound. `start()`
  confirms playback within a timeout, raising on a dead state (404/500 URL →
  failure, not false success) — see `_confirm_playback_started` below.

- **Event-silent renderers** (e.g. HiFiBerryOS) never emit `LAST_CHANGE`. The
  backend's `query_transport_state()` actively polls `GetTransportInfo` (bounded
  by `_TRANSPORT_POLL_TIMEOUT`); `get_status` calls `refresh_state()` →
  `backend.refresh()` first. Their *polled* `TransportState` is also unreliable
  (observed reporting PLAYING while silent, and not reporting a clean STOPPED on
  a failed stream), so `_confirm_playback_started` does NOT trust it: for an
  event-silent renderer it confirms via the playback **position advancing**
  (`backend.query_playback()` → `(state, position)` in one `async_update`),
  raising on a polled-dead state or a position that never advances. Event-
  emitting renderers (OpenHome/Sonos) keep the evented-state confirm and are
  never position-polled.

- **Volume/mute bypass the DmrDevice abstraction** (in `AvTransportBackend`).
  They call `RenderingControl` actions directly (raw 0–100) instead of
  `DmrDevice` helpers, because some renderers (Linn) advertise a bogus volume max
  (2^31-1) that makes `async_set_volume_level` send a huge value and read
  `volume_level`/`has_volume_mute` as None/False. `_volume_scale()` treats an
  insane range as 0–100. Read the comment block above `_rendering_control()`
  before touching volume. (Once `OpenHomeBackend` lands, Linn uses its OpenHome
  Volume service and this is only the AVTransport fallback.)

- **DIDL-Lite metadata** (`didl.py`) is built per track in `QueueSession` and
  passed to the backend; audio uses `MusicTrack`, video uses `Movie`. The plan
  migrates this to a hybrid library + per-device-family protocolInfo strategy
  (strict TVs need exact `DLNA.ORG_PN` the library won't infer).

## Conventions

- Logging goes to **stderr** — stdout is reserved for the MCP stdio protocol.
  Never `print()` to stdout.
- Async throughout; tests use `asyncio_mode=auto` so no `@pytest.mark.asyncio`
  is needed.
- Tests mock the backend's `_dmr` / RenderingControl layer rather than hitting a
  real renderer (`_mock_dmr_with_rc`, `_connected_backend` in
  `tests/test_server.py`); queue-reaction tests drive
  `QueueSession._on_transport_event(state, uri)` directly.
- `DLNA_LISTEN_IP` (read in `control_point.detect_local_ip`) overrides the
  auto-detected local IP for the UPnP event-callback URL — needed when local-IP
  detection picks the wrong interface.

## Deployment note

SSDP multicast requires LAN access, so when the Renfield backend runs in
Docker this server must run **on the host** (streamable-http) and be reached via
`host.docker.internal:9091/mcp`. It cannot discover renderers from inside a
container.
