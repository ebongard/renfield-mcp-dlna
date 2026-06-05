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

Three source modules under `src/renfield_mcp_dlna/`, layered:

- **`server.py`** — thin FastMCP tool layer. Each `@mcp.tool()` resolves a
  renderer by name, looks up its session, delegates to `queue_manager`, and
  returns a `{"success": bool, ...}` dict. Tools never raise to the client;
  they catch and return error dicts. `main()` selects transport from
  `MCP_TRANSPORT`.
- **`discovery.py`** — SSDP M-SEARCH (raw UDP multicast to
  `239.255.255.250:1900`) + device-description XML parsing. Holds a 5-min
  module-level renderer cache. `find_renderer()` matches by case-insensitive
  substring on friendly name.
- **`queue_manager.py`** — the real logic. `QueueSession` is a per-renderer
  state machine driving playback; module-level `_sessions` dict (keyed by
  renderer UDN) is the session registry, and a single shared UPnP notify
  server / event handler is started lazily and torn down when the last session
  ends.

### Key behaviors that span files

- **Gapless vs. auto-advance.** Renderers advertising `SetNextAVTransportURI`
  (the `supports_next` flag, detected by parsing the AVTransport SCPD during
  discovery) get the next track preloaded for gapless transition. Renderers
  without it are auto-advanced in `_on_event` when a `STOPPED` event arrives
  *after* the current track actually reached `PLAYING` (the `played` gate).
  The `_advancing` flag dedupes duplicate `STOPPED` events.

- **Transport state is the source of truth.** `status()` reports what the
  renderer actually says (`PLAYING`/`STOPPED`/`NO_MEDIA_PRESENT`), never
  "playing" just because a session is bound. `_on_event` consumes AVTransport
  `LAST_CHANGE` events. Event delivery gives a **list** of state-variable
  objects (not a dict) — `_on_event` folds it to a name→value map; getting this
  wrong silently breaks all transition detection. `start()` confirms playback
  began within a timeout, raising if the renderer reports a dead state (so a
  404 media URL surfaces as a failure rather than a false success).

- **Event-silent renderers.** Some renderers (e.g. HiFiBerryOS) never emit
  `LAST_CHANGE`. For those, `_query_transport_state()` actively polls
  `GetTransportInfo` (bounded by `_TRANSPORT_POLL_TIMEOUT`), and `get_status`
  calls `refresh_state()` before reporting.

- **Volume/mute bypass the DmrDevice abstraction.** They call the
  `RenderingControl` service actions directly (raw 0–100 `SetVolume`/`GetVolume`
  /`SetMute`/`GetMute`) instead of `async_upnp_client`'s `DmrDevice` helpers.
  Reason: some renderers (Linn) advertise a bogus volume max (2^31-1), which
  makes `DmrDevice.async_set_volume_level` send a huge value and read
  `volume_level`/`has_volume_mute` as None/False. `_volume_scale()` treats an
  insane advertised range as 0–100. See the long comment block above
  `_rendering_control()` before changing any volume code.

- **DIDL-Lite metadata** (`didl.py`) is built per track and passed to
  `SetAVTransportURI`/`SetNextAVTransportURI`; audio uses `MusicTrack`, video
  uses `Movie`.

## Conventions

- Logging goes to **stderr** — stdout is reserved for the MCP stdio protocol.
  Never `print()` to stdout.
- Async throughout; tests use `asyncio_mode=auto` so no `@pytest.mark.asyncio`
  is needed.
- Tests mock the `_dmr` / RenderingControl layer rather than hitting a real
  renderer (see `_mock_dmr_with_rc` in `tests/test_server.py`).
- `DLNA_LISTEN_IP` overrides the auto-detected local IP used for the UPnP
  event-callback URL — needed when local-IP detection picks the wrong interface.

## Deployment note

SSDP multicast requires LAN access, so when the Renfield backend runs in
Docker this server must run **on the host** (streamable-http) and be reached via
`host.docker.internal:9091/mcp`. It cannot discover renderers from inside a
container.
