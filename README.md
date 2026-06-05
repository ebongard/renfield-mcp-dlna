# renfield-mcp-dlna

MCP server that acts as a UPnP/DLNA **control point**: discovers renderers **and**
MediaServers via SSDP, plays to renderers with gapless queues, and browses content
libraries. Per-device-class backends cover standard DLNA AVTransport, OpenHome
(Linn, native device-side queue), and Sonos (via `soco`).

Content can come from any MediaServer's ContentDirectory (e.g. Jellyfin's built-in
DLNA server) via `play_from_server`, or from caller-supplied URLs via `play_tracks`.

## Tools

### Renderers (playback)

| Tool | Description |
|------|-------------|
| `list_renderers` | Discover DLNA renderers on the network (5-min cache); reports `supports_queue` |
| `play_tracks` | Play a list of tracks with gapless queue (SetNextAVTransportURI) |
| `stop` | Stop playback and clear queue |
| `pause` / `resume` | Pause / resume playback |
| `next_track` / `previous_track` | Skip to next / previous track |
| `seek` | Seek to a position (seconds) within the current track |
| `set_play_mode` | Set play mode: `normal`/`repeat_one`/`repeat_all`/`shuffle`/`random` (single UPnP play mode, gated on `valid_play_modes`) |
| `get_status` | State, track info, queue position, **position/duration/capabilities/volume/muted/valid_play_modes** |
| `set_volume` / `get_volume` | Set / get volume (0-100) |
| `set_mute` / `get_mute` | Mute / unmute / query mute |

### MediaServers (content libraries)

| Tool | Description |
|------|-------------|
| `list_servers` | Discover DLNA MediaServers (ContentDirectory) on the network |
| `browse_server` | List a library container's children (paginated; `object_id` `"0"` = root) |
| `search_server` | Search a library by title (capability-gated) |
| `play_from_server` | Resolve a library object (album/playlist/track) and play it on a renderer — no caller URLs needed |

## Installation

```bash
pip install .
```

## Usage

### stdio (default — for MCP subprocess)

```bash
renfield-mcp-dlna
# or
python -m renfield_mcp_dlna
```

### streamable-http (standalone service)

```bash
MCP_TRANSPORT=streamable-http MCP_PORT=9091 renfield-mcp-dlna
```

The server listens on `http://0.0.0.0:9091/mcp` (configurable via `MCP_HOST` and `MCP_PORT`).

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_TRANSPORT` | `stdio` | Transport: `stdio` or `streamable-http` |
| `MCP_HOST` | `0.0.0.0` | Bind address (streamable-http only) |
| `MCP_PORT` | `9091` | Listen port (streamable-http only) |
| `DLNA_LISTEN_IP` | auto | Override the local IP used for the UPnP event-callback URL (multi-homed hosts) |
| `RENFIELD_OPENHOME` | on | OpenHome renderers (Linn) use the native Playlist backend by default (hardware-validated); set `0` to fall back to AVTransport |
| `RENFIELD_SONOS` | unset | `1` routes Sonos renderers to the `soco`-backed backend (provisional; needs `.[sonos]`) |

## Deployment

SSDP multicast (`239.255.255.250:1900`) requires **LAN access**, so this server
must share the host's network — either run it directly on the host, or in a
container with **host networking** (`docker run --network host` / Kubernetes
`hostNetwork: true`). A container on a bridge/NAT network can't see SSDP and won't
discover renderers. (Running on the long-lived `streamable-http` transport also
enables the passive SSDP live cache + session watchdog.)

### Docker

A dedicated image is built from the included `Dockerfile` (defaults to
`streamable-http` on `0.0.0.0:9091`):

```bash
docker build -t renfield-mcp-dlna .
docker run --rm --network host renfield-mcp-dlna
```

For Sonos support, build with the extra: add `RUN pip install '.[sonos]'` or set
`RENFIELD_SONOS=1` against an image that includes `soco`.

### systemd Service

```ini
# /etc/systemd/system/renfield-mcp-dlna.service
[Unit]
Description=Renfield DLNA MCP Server
After=network.target

[Service]
Type=simple
User=your-user
Environment=MCP_TRANSPORT=streamable-http
Environment=MCP_PORT=9091
Environment=MCP_HOST=0.0.0.0
ExecStart=/home/your-user/.local/bin/renfield-mcp-dlna
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now renfield-mcp-dlna
```

### Renfield Configuration

In `config/mcp_servers.yaml`:

```yaml
- name: dlna
  transport: streamable_http
  url: "${DLNA_MCP_URL:-http://host.docker.internal:9091/mcp}"
  enabled: "${DLNA_MCP_ENABLED:-false}"
```

In `.env`:

```bash
DLNA_MCP_ENABLED=true
# DLNA_MCP_URL=http://host.docker.internal:9091/mcp  # default
```

## Architecture

```
DLNA Renderers (LAN)          DLNA MCP Server (Host)         Renfield Backend (Docker)
  Linn, Samsung TV, etc.       SSDP discovery + UPnP           MCP client
        ↑                            ↑                              ↑
        │  SSDP Multicast            │  streamable-http             │
        │  239.255.255.250:1900      │  :9091/mcp                   │
        └────────────────────────────┘                              │
                                     └──────────────────────────────┘
                                      host.docker.internal:9091/mcp
```

## Dependencies

- `mcp>=1.26.0` — Model Context Protocol SDK
- `async-upnp-client>=0.47.0` — UPnP/SSDP client (DmrDevice/DmsDevice, play modes, metadata negotiation)
- `python-didl-lite>=1.4.0` — DIDL-Lite XML for UPnP metadata
- `aiohttp>=3.9.0` — Async HTTP for device description fetching
- `defusedxml>=0.7` — hardened XML parsing of (LAN-spoofable) device descriptions
- `ifaddr>=0.2` — local-interface enumeration for multi-interface SSDP discovery

Optional: `pip install '.[sonos]'` adds `soco>=0.30` for the (provisional) Sonos backend.
