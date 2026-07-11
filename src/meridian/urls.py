"""URL building and QR code generation for VLESS connections."""

from __future__ import annotations

import base64
import io

import segno

from meridian.credentials import ServerCredentials
from meridian.models import ProtocolURL, RelayURLSet
from meridian.protocols import PROTOCOLS

PUBLIC_PROTOCOL_KEYS = {"xhttp"}


def build_protocol_urls(
    name: str,
    reality_uuid: str,
    wss_uuid: str,
    creds: ServerCredentials,
    server_name: str = "",
) -> list[ProtocolURL]:
    """Build VLESS connection URLs for a client across all active protocols.

    Iterates over ``PROTOCOLS`` in registry order and produces a
    ``ProtocolURL`` for every protocol whose URL can be built given the
    supplied arguments.  Protocols that are not active (e.g. WSS without a
    domain, XHTTP without a path) are omitted from the returned list.

    Args:
        name: Client display name (used in URL fragment).
        reality_uuid: UUID for Reality and XHTTP connections.
        wss_uuid: UUID for WSS connection (empty if not domain mode).
        creds: Server credentials with protocol configs.

    Returns:
        Ordered list of ``ProtocolURL`` objects, one per active protocol.
    """
    result: list[ProtocolURL] = []
    for proto in PROTOCOLS.values():
        url = proto.build_url_from_creds(reality_uuid, wss_uuid, creds, name, server_name=server_name)
        if url:
            result.append(ProtocolURL(key=proto.key, label=proto.display_label, url=url))
    return result


def public_protocol_urls(protocol_urls: list[ProtocolURL]) -> list[ProtocolURL]:
    """Return the connection URLs exposed to end users.

    PridVPN publishes only XHTTP. Other inbounds may exist internally for
    provisioning compatibility, but they are not shown in pages/subscriptions.
    """
    return [p for p in protocol_urls if p.key in PUBLIC_PROTOCOL_KEYS]


def public_relay_url_sets(relay_entries: list[RelayURLSet]) -> list[RelayURLSet]:
    """Filter relay URL sets to public protocols only."""
    result: list[RelayURLSet] = []
    for relay in relay_entries:
        urls = public_protocol_urls(relay.urls)
        if urls:
            result.append(RelayURLSet(relay_ip=relay.relay_ip, relay_name=relay.relay_name, urls=urls))
    return result


def preferred_public_urls(
    protocol_urls: list[ProtocolURL],
    relay_entries: list[RelayURLSet] | None = None,
) -> list[ProtocolURL]:
    """Return the URLs that should be published to subscriptions.

    Split relays terminate the client connection on the relay and make the
    routing decision server-side. When present, they are the canonical public
    URL; direct exit URLs stay available on connection pages as backup but are
    not duplicated in subscription feeds.
    """
    if relay_entries:
        split_urls: list[ProtocolURL] = []
        for relay in relay_entries:
            for purl in relay.urls:
                if purl.key in PUBLIC_PROTOCOL_KEYS and purl.url:
                    split_urls.append(purl)
        if split_urls:
            return split_urls
    return public_protocol_urls(protocol_urls)


def build_relay_urls(
    name: str,
    reality_uuid: str,
    wss_uuid: str,
    creds: ServerCredentials,
    relay_ip: str,
    relay_name: str = "",
    relay_port: int = 443,
    server_name: str = "",
    relay_sni: str = "",
) -> RelayURLSet:
    """Build connection URLs that route through a relay node.

    A dumb L4 relay forwards TCP transparently, so TLS goes end-to-end
    to the exit server.  All protocols work if we set explicit ``sni=``
    parameters pointing to the exit's TLS certificate identity:

    - **Reality**: uses relay-specific SNI when available (per-relay
      Xray inbound on exit handles it). Falls back to exit's SNI.
    - **XHTTP**: add ``sni=<exit_ip_or_domain>`` so nginx's cert matches.
    - **WSS**: add ``sni=<domain>`` + ``host=<domain>`` (domain mode only).

    Args:
        name: Client display name.
        reality_uuid: UUID for Reality and XHTTP connections.
        wss_uuid: UUID for WSS connection (empty if not domain mode).
        creds: Exit server credentials (SNI, keys, paths).
        relay_ip: Relay node IP address (substituted for exit IP).
        relay_name: Friendly relay name (used in URL fragment).
        relay_port: Relay listen port (default 443).
        relay_sni: Relay-specific SNI for Reality (empty = use exit's SNI).

    Returns:
        A ``RelayURLSet`` with all active protocol URLs via this relay.
    """
    relay_label = relay_name or relay_ip
    urls: list[ProtocolURL] = []

    for proto in PROTOCOLS.values():
        url = proto.build_relay_url(
            reality_uuid,
            wss_uuid,
            creds,
            name,
            relay_ip,
            relay_port,
            relay_sni=relay_sni,
            relay_name=relay_name,
            server_name=server_name,
            xhttp_path=getattr(creds, "_relay_xhttp_path", ""),
            public_host=getattr(creds, "_relay_public_host", ""),
        )
        if url:
            label = f"{proto.display_label} (via {relay_label})"
            urls.append(ProtocolURL(key=proto.key, label=label, url=url))

    return RelayURLSet(relay_ip=relay_ip, relay_name=relay_name, urls=urls)


def build_all_relay_urls(
    name: str,
    reality_uuid: str,
    wss_uuid: str,
    creds: ServerCredentials,
    server_name: str = "",
) -> list[RelayURLSet]:
    """Build relay URL sets for all relays attached to the exit server.

    Returns an empty list if no relays are configured.
    """
    return [
        _build_relay_urls_for_entry(
            name,
            reality_uuid,
            wss_uuid,
            creds,
            relay,
            server_name=server_name,
        )
        for relay in creds.relays
    ]


def _build_relay_urls_for_entry(
    name: str,
    reality_uuid: str,
    wss_uuid: str,
    creds: ServerCredentials,
    relay: object,
    server_name: str = "",
) -> RelayURLSet:
    """Build relay URLs using extended RelayEntry fields when available."""
    relay_ip = getattr(relay, "ip", "")
    relay_name = getattr(relay, "name", "")
    relay_port = getattr(relay, "port", 443)
    relay_sni = getattr(relay, "sni", "")
    xhttp_path = getattr(relay, "xhttp_path", "")
    public_host = getattr(relay, "public_host", "")

    # Keep build_relay_urls public and backwards-compatible without expanding
    # its signature for callers/tests that construct relay URLs directly.
    setattr(creds, "_relay_xhttp_path", xhttp_path)
    setattr(creds, "_relay_public_host", public_host)
    try:
        return build_relay_urls(
            name,
            reality_uuid,
            wss_uuid,
            creds,
            relay_ip,
            relay_name,
            relay_port,
            server_name=server_name,
            relay_sni=relay_sni,
        )
    finally:
        if hasattr(creds, "_relay_xhttp_path"):
            delattr(creds, "_relay_xhttp_path")
        if hasattr(creds, "_relay_public_host"):
            delattr(creds, "_relay_public_host")


def generate_qr_terminal(url: str) -> str:
    """Generate a QR code for terminal display."""
    try:
        qr = segno.make(url)
        buf = io.StringIO()
        qr.terminal(out=buf, compact=True)
        return buf.getvalue()
    except (ValueError, OSError):
        return ""


def generate_qr_base64(url: str) -> str:
    """Generate a QR code as base64-encoded PNG for HTML embedding."""
    try:
        qr = segno.make(url)
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=12)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except (ValueError, OSError):
        return ""
