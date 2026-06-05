# Dedicated renfield-mcp-dlna image — a small UPnP control-point server.
#
# Replaces the previous arrangement where deploy/dlna-mcp ran the 3.5 GB backend
# image with `command: renfield-mcp-dlna`. The backend never imports this package
# (it talks to it over HTTP), so a standalone image lets pip install the real
# deps (async-upnp-client / defusedxml / ifaddr) instead of hand-mirroring them
# into the backend's requirements.txt, and decouples release cadence.
#
# Runs on hostNetwork (SSDP multicast) — see k8s/dlna-mcp.yaml in the renfield repo.
FROM python:3.11-slim

WORKDIR /app

# All runtime deps ship as pure-python or manylinux wheels (aiohttp,
# async-upnp-client, defusedxml, ifaddr) — no compiler toolchain needed.
RUN pip install --no-cache-dir --upgrade "pip>=25.3"

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# streamable-http on 0.0.0.0:9091 by default (k8s overrides via env if needed).
ENV MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=9091

EXPOSE 9091

ENTRYPOINT ["renfield-mcp-dlna"]
