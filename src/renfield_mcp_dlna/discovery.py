"""SSDP discovery and renderer cache for DLNA MediaRenderers."""

import asyncio
import logging
import socket
import struct
import time
from dataclasses import dataclass

import aiohttp

# defusedxml: device descriptions come from LAN devices and UPnP is spoofable,
# so parse untrusted XML with the hardened parser (blocks billion-laughs entity
# expansion / XXE that stdlib ElementTree allows).
import defusedxml.ElementTree as ET  # noqa: N817
from defusedxml.common import DefusedXmlException

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


_OPENHOME_PLAYLIST_TYPE = "urn:av-openhome-org:service:Playlist:1"


@dataclass
class DlnaRenderer:
    """Discovered DLNA MediaRenderer.

    Identity fields (manufacturer/model) feed the backend factory's
    device-class selection; is_openhome flags renderers (Linn et al.) that
    expose the av-openhome-org Playlist service for native device-side queues.
    """

    name: str
    udn: str
    location: str
    supports_next: bool
    av_transport_control_url: str
    rendering_control_url: str = ""
    base_url: str = ""
    manufacturer: str = ""
    model_name: str = ""
    is_openhome: bool = False
    is_sonos: bool = False


_renderer_cache: list[DlnaRenderer] = []
_cache_time: float = 0

_CONTENT_DIRECTORY_PREFIX = "urn:schemas-upnp-org:service:ContentDirectory:"


@dataclass
class DlnaServer:
    """Discovered DLNA MediaServer (exposes a ContentDirectory to browse)."""

    name: str
    udn: str
    location: str
    content_directory_control_url: str
    base_url: str = ""
    manufacturer: str = ""
    model_name: str = ""


_server_cache: list[DlnaServer] = []
_server_cache_time: float = 0


def _build_msearch(search_target: str = _SEARCH_TARGET) -> bytes:
    """Build an SSDP M-SEARCH request."""
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {_SSDP_ADDR}:{_SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 5\r\n"
        f"ST: {search_target}\r\n"
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


async def _ssdp_search_single(
    search_target: str, timeout: float = 6.0
) -> list[str]:
    """Send SSDP M-SEARCH for a single ST and collect LOCATION URLs."""
    locations: list[str] = []
    msg = _build_msearch(search_target)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_MULTICAST_TTL,
        struct.pack("b", 4),
    )
    # Keep socket blocking — recv is run in executor thread.
    # select() provides the timeout; recv() blocks until data arrives.
    sock.setblocking(True)

    loop = asyncio.get_running_loop()

    # Send M-SEARCH twice for reliability
    for _ in range(2):
        await loop.run_in_executor(
            None, lambda: sock.sendto(msg, (_SSDP_ADDR, _SSDP_PORT))
        )
        await asyncio.sleep(0.1)

    import select

    def _recv_with_select(s: socket.socket, wait: float) -> bytes | None:
        """Block up to *wait* seconds for data using select()."""
        ready, _, _ = select.select([s], [], [], wait)
        if ready:
            return s.recv(4096)
        return None

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        wait = min(remaining, 1.0)
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, _recv_with_select, sock, wait),
                timeout=wait + 0.5,  # small grace for thread scheduling
            )
            if data is None:
                continue  # select timed out, keep waiting
            loc = _parse_location(data.decode("utf-8", errors="ignore"))
            if loc and loc not in locations:
                locations.append(loc)
        except (TimeoutError, asyncio.TimeoutError):
            continue
        except OSError:
            break

    sock.close()
    return locations


async def _ssdp_search(timeout: float = 5.0) -> list[str]:
    """Send SSDP M-SEARCH for MediaRenderer and rootdevice, merge results.

    Some devices (e.g. Linn/ohNet) don't respond to the MediaRenderer search
    target but do support AVTransport. Searching for upnp:rootdevice as well
    catches these devices.  Filtering by AVTransport happens later.
    """
    results = await asyncio.gather(
        _ssdp_search_single(_SEARCH_TARGET, timeout),
        _ssdp_search_single("upnp:rootdevice", timeout),
    )
    # Merge and deduplicate
    seen: set[str] = set()
    locations: list[str] = []
    for loc_list in results:
        for loc in loc_list:
            if loc not in seen:
                seen.add(loc)
                locations.append(loc)
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
    except (ET.ParseError, DefusedXmlException):
        return None

    device = root.find(f"{{{_NS_DEVICE}}}device")
    if device is None:
        logger.debug(f"No <device> element in {location}")
        return None

    friendly_name = device.findtext(f"{{{_NS_DEVICE}}}friendlyName", "")
    udn = device.findtext(f"{{{_NS_DEVICE}}}UDN", "")
    if not udn:
        logger.debug(f"No UDN for device '{friendly_name}' at {location}")
        return None

    # Identity drives backend-class selection downstream.
    manufacturer = device.findtext(f"{{{_NS_DEVICE}}}manufacturer", "")
    model_name = device.findtext(f"{{{_NS_DEVICE}}}modelName", "")

    base_url = _base_url_from_location(location)
    av_control_url = ""
    rc_control_url = ""
    is_openhome = False

    service_list = device.find(f"{{{_NS_DEVICE}}}serviceList")
    if service_list is None:
        logger.debug(f"No serviceList for '{friendly_name}' at {location}")
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
        elif service_type == _OPENHOME_PLAYLIST_TYPE:
            # OpenHome renderer (Linn et al.): owns the queue device-side.
            is_openhome = True

    if not av_control_url:
        logger.debug(f"No AVTransport service for '{friendly_name}' at {location}")
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
        manufacturer=manufacturer,
        model_name=model_name,
        is_openhome=is_openhome,
        is_sonos="sonos" in manufacturer.lower(),
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
    except (ET.ParseError, DefusedXmlException):
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
    return _match_by_name(await discover_renderers(), name)


# ---------------------------------------------------------------------------
# MediaServer (ContentDirectory) discovery
# ---------------------------------------------------------------------------


async def _fetch_server_description(
    session: aiohttp.ClientSession, location: str
) -> DlnaServer | None:
    """Fetch a device description and parse it as a MediaServer (one that
    exposes a ContentDirectory service). Returns None for non-servers."""
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
    except (ET.ParseError, DefusedXmlException):
        return None

    device = root.find(f"{{{_NS_DEVICE}}}device")
    if device is None:
        return None

    udn = device.findtext(f"{{{_NS_DEVICE}}}UDN", "")
    if not udn:
        return None
    friendly_name = device.findtext(f"{{{_NS_DEVICE}}}friendlyName", "")
    manufacturer = device.findtext(f"{{{_NS_DEVICE}}}manufacturer", "")
    model_name = device.findtext(f"{{{_NS_DEVICE}}}modelName", "")
    base_url = _base_url_from_location(location)

    service_list = device.find(f"{{{_NS_DEVICE}}}serviceList")
    if service_list is None:
        return None

    cd_control_url = ""
    for service in service_list.findall(f"{{{_NS_DEVICE}}}service"):
        service_type = service.findtext(f"{{{_NS_DEVICE}}}serviceType", "")
        if service_type.startswith(_CONTENT_DIRECTORY_PREFIX):
            cd_control_url = service.findtext(f"{{{_NS_DEVICE}}}controlURL", "") or ""
            break

    if not cd_control_url:
        return None  # not a MediaServer
    if not cd_control_url.startswith("http"):
        cd_control_url = base_url + cd_control_url

    return DlnaServer(
        name=friendly_name,
        udn=udn,
        location=location,
        content_directory_control_url=cd_control_url,
        base_url=base_url,
        manufacturer=manufacturer,
        model_name=model_name,
    )


async def discover_servers(force: bool = False) -> list[DlnaServer]:
    """Discover DLNA MediaServers on the network (5-minute cache)."""
    global _server_cache, _server_cache_time

    if not force and _server_cache and (time.time() - _server_cache_time) < CACHE_TTL:
        return _server_cache

    logger.info("Starting SSDP discovery for DLNA servers...")
    locations = await _ssdp_search()

    servers: list[DlnaServer] = []
    async with aiohttp.ClientSession() as session:
        tasks = [_fetch_server_description(session, loc) for loc in locations]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, DlnaServer):
                servers.append(result)
            elif isinstance(result, Exception):
                logger.debug(f"Server description fetch failed: {result}")

    _server_cache = servers
    _server_cache_time = time.time()
    logger.info(
        f"Discovered {len(servers)} server(s): "
        + ", ".join(s.name for s in servers)
    )
    return servers


async def find_server(name: str) -> DlnaServer | None:
    """Find a MediaServer by case-insensitive substring match on friendly name."""
    return _match_by_name(await discover_servers(), name)


def _match_by_name(devices: list, name: str):
    """Exact (case-insensitive) match first, then substring, on .name."""
    name_lower = name.lower()
    for d in devices:
        if d.name.lower() == name_lower:
            return d
    for d in devices:
        if name_lower in d.name.lower():
            return d
    return None
