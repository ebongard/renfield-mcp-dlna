"""SSDP discovery and renderer cache for DLNA MediaRenderers."""

import asyncio
import logging
import socket
import struct
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import aiohttp

logger = logging.getLogger(__name__)

# UPnP XML namespaces
_NS_DEVICE = "urn:schemas-upnp-org:device-1-0"
_NS_SERVICE = "urn:schemas-upnp-org:service-1-0"
_AVTRANSPORT_TYPE = "urn:schemas-upnp-org:service:AVTransport:1"
_RENDERING_CONTROL_TYPE = "urn:schemas-upnp-org:service:RenderingControl:1"

# SSDP constants
_SSDP_ADDR = "239.255.255.250"
_SSDP_PORT = 1900
_SEARCH_TARGET = "urn:schemas-upnp-org:device:MediaRenderer:1"

# Cache
CACHE_TTL = 300  # 5 minutes


@dataclass
class DlnaRenderer:
    """Discovered DLNA MediaRenderer."""

    name: str
    udn: str
    location: str
    supports_next: bool
    av_transport_control_url: str
    rendering_control_url: str = ""
    base_url: str = ""


_renderer_cache: list[DlnaRenderer] = []
_cache_time: float = 0


def _build_msearch() -> bytes:
    """Build an SSDP M-SEARCH request."""
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {_SSDP_ADDR}:{_SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 3\r\n"
        f"ST: {_SEARCH_TARGET}\r\n"
        "\r\n"
    ).encode("utf-8")


def _parse_location(response: str) -> str | None:
    """Extract LOCATION header from SSDP response."""
    for line in response.split("\r\n"):
        if line.lower().startswith("location:"):
            return line.split(":", 1)[1].strip()
    return None


def _base_url_from_location(location: str) -> str:
    """Extract base URL (scheme + host + port) from a full URL."""
    from urllib.parse import urlparse

    parsed = urlparse(location)
    return f"{parsed.scheme}://{parsed.netloc}"


async def _ssdp_search(timeout: float = 4.0) -> list[str]:
    """Send SSDP M-SEARCH and collect LOCATION URLs."""
    locations: list[str] = []
    msg = _build_msearch()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_MULTICAST_TTL,
        struct.pack("b", 4),
    )
    sock.setblocking(False)

    loop = asyncio.get_running_loop()

    # Send M-SEARCH twice for reliability
    for _ in range(2):
        await loop.run_in_executor(
            None, lambda: sock.sendto(msg, (_SSDP_ADDR, _SSDP_PORT))
        )
        await asyncio.sleep(0.1)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: sock.recv(4096)),
                timeout=min(remaining, 1.0),
            )
            loc = _parse_location(data.decode("utf-8", errors="ignore"))
            if loc and loc not in locations:
                locations.append(loc)
        except (TimeoutError, asyncio.TimeoutError):
            continue
        except OSError:
            break

    sock.close()
    return locations


async def _fetch_device_description(
    session: aiohttp.ClientSession, location: str
) -> DlnaRenderer | None:
    """Fetch device description XML and parse renderer info."""
    try:
        async with session.get(location, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return None
            xml_text = await resp.text()
    except Exception as e:
        logger.debug(f"Failed to fetch {location}: {e}")
        return None

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    device = root.find(f"{{{_NS_DEVICE}}}device")
    if device is None:
        return None

    friendly_name = device.findtext(f"{{{_NS_DEVICE}}}friendlyName", "")
    udn = device.findtext(f"{{{_NS_DEVICE}}}UDN", "")
    if not udn:
        return None

    base_url = _base_url_from_location(location)
    av_control_url = ""
    rc_control_url = ""

    service_list = device.find(f"{{{_NS_DEVICE}}}serviceList")
    if service_list is None:
        return None

    for service in service_list.findall(f"{{{_NS_DEVICE}}}service"):
        service_type = service.findtext(f"{{{_NS_DEVICE}}}serviceType", "")
        control_url = service.findtext(f"{{{_NS_DEVICE}}}controlURL", "")
        scpd_url = service.findtext(f"{{{_NS_DEVICE}}}SCPDURL", "")

        if service_type == _AVTRANSPORT_TYPE:
            av_control_url = control_url or ""
            # Check SCPD for SetNextAVTransportURI support
            supports_next = await _check_set_next_support(
                session, base_url, scpd_url or ""
            )
        elif service_type == _RENDERING_CONTROL_TYPE:
            rc_control_url = control_url or ""

    if not av_control_url:
        return None

    # Resolve relative URLs
    if av_control_url and not av_control_url.startswith("http"):
        av_control_url = base_url + av_control_url
    if rc_control_url and not rc_control_url.startswith("http"):
        rc_control_url = base_url + rc_control_url

    return DlnaRenderer(
        name=friendly_name,
        udn=udn,
        location=location,
        supports_next=supports_next,
        av_transport_control_url=av_control_url,
        rendering_control_url=rc_control_url,
        base_url=base_url,
    )


async def _check_set_next_support(
    session: aiohttp.ClientSession, base_url: str, scpd_path: str
) -> bool:
    """Check if the AVTransport SCPD lists SetNextAVTransportURI."""
    if not scpd_path:
        return False

    url = scpd_path if scpd_path.startswith("http") else base_url + scpd_path
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return False
            xml_text = await resp.text()
    except Exception:
        return False

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False

    # Search for action name in SCPD
    for action in root.iter(f"{{{_NS_SERVICE}}}action"):
        name = action.findtext(f"{{{_NS_SERVICE}}}name", "")
        if name == "SetNextAVTransportURI":
            return True

    return False


async def discover_renderers(force: bool = False) -> list[DlnaRenderer]:
    """Discover DLNA MediaRenderers on the network.

    Uses a 5-minute cache unless force=True.
    """
    global _renderer_cache, _cache_time

    if not force and _renderer_cache and (time.time() - _cache_time) < CACHE_TTL:
        return _renderer_cache

    logger.info("Starting SSDP discovery for DLNA renderers...")
    locations = await _ssdp_search()
    logger.info(f"SSDP found {len(locations)} device location(s)")

    renderers: list[DlnaRenderer] = []
    async with aiohttp.ClientSession() as session:
        tasks = [_fetch_device_description(session, loc) for loc in locations]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, DlnaRenderer):
                renderers.append(result)
            elif isinstance(result, Exception):
                logger.debug(f"Device description fetch failed: {result}")

    _renderer_cache = renderers
    _cache_time = time.time()
    logger.info(
        f"Discovered {len(renderers)} renderer(s): "
        + ", ".join(f"{r.name} (next={r.supports_next})" for r in renderers)
    )
    return renderers


async def find_renderer(name: str) -> DlnaRenderer | None:
    """Find a renderer by case-insensitive substring match on friendly name."""
    renderers = await discover_renderers()
    name_lower = name.lower()

    # Exact match first
    for r in renderers:
        if r.name.lower() == name_lower:
            return r

    # Substring match
    for r in renderers:
        if name_lower in r.name.lower():
            return r

    return None
