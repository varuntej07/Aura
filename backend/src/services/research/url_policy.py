"""The public-URL policy for research page reading.

This is the first URL-safety code in the backend. Every other outbound HTTP call in
`src/` targets a hardcoded host (Brave, Dodo, Deepgram, Telegram, newsdata), so nothing
here had an existing pattern to reuse.

Two properties matter:

1. **Fail closed.** Every check refuses on doubt. A URL that cannot be parsed, whose
   host cannot be resolved, or whose resolved address is not globally routable is
   rejected with a stable reason, and the caller records a gap rather than raising.

2. **This is Aura's own control, not a proxy for the provider's.** The page reader runs
   inside Firecrawl's network, not ours, so a private address here could never reach
   Aura's own metadata service. The DNS check is still done, for two reasons: Aura must
   not disclose an internal hostname to a third party, and Aura must never treat a
   provider's SSRF protection as its own. The check has a real TOCTOU gap (DNS can
   change between this call and the provider's fetch) and is defense in depth, not a
   guarantee.

There is no hostname allowlist and no content pattern matching here. The only closed
sets are structural: URL schemes, reserved DNS suffixes, and signed-URL query keys.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import UrlRejectReason, UrlVerdict

_MAX_URL_LEN = 2048

# Suffixes reserved for private/local naming. A public research source is never here.
_RESERVED_SUFFIXES = (".local", ".localhost", ".internal", ".home.arpa", ".onion")

# Hosts that name an internal service directly rather than by address.
_METADATA_HOSTS = frozenset({
    "localhost",
    "metadata",
    "metadata.google.internal",
    "instance-data",
})

# Query keys that mean the URL itself carries a credential. A closed set on purpose:
# a pattern matcher here would create false confidence, and the real boundary is that
# Aura never forwards a credential-bearing URL to a third-party reader at all.
_SIGNED_QUERY_KEYS = frozenset({
    "x-amz-signature",
    "x-amz-credential",
    "x-amz-security-token",
    "x-goog-signature",
    "x-goog-credential",
    "sig",
    "signature",
    "token",
    "access_token",
    "accesstoken",
    "auth",
    "authorization",
    "se",
    "sp",
})

# Dropped during canonicalization so the same page discovered by two queries dedupes
# to one key. Everything else in the query string is preserved verbatim, because it
# may be load bearing (?id=, ?page=).
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = frozenset({
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref_src",
})

_DEFAULT_PORTS = {"https": 443, "http": 80}


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered in _TRACKING_KEYS or lowered.startswith(_TRACKING_PREFIXES)


def canonicalize(raw_url: str) -> str:
    """Stable dedupe key for a URL. Never raises; returns the input on a parse failure.

    Lowercases scheme and host, strips the fragment, strips a default port and a
    trailing dot on the host, and drops tracking parameters. Deliberately does NOT
    touch the path (case and trailing slash can both be significant).
    """
    try:
        parts = urlsplit(raw_url.strip())
    except ValueError:
        return raw_url.strip()

    host = (parts.hostname or "").rstrip(".")
    if not host:
        return raw_url.strip()

    netloc = f"[{host}]" if ":" in host else host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and port != _DEFAULT_PORTS.get(parts.scheme.lower()):
        netloc = f"{netloc}:{port}"

    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if not _is_tracking_param(k)]
    )
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, query, ""))


def _unwrap(address: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """Resolve v6 tunnelling forms down to the v4 address they actually carry.

    Without this, `::ffff:127.0.0.1` and 6to4/Teredo wrappers can present as global
    while pointing at a private v4 target.
    """
    if isinstance(address, ipaddress.IPv6Address):
        for embedded in (address.ipv4_mapped, address.sixtofour):
            if embedded is not None:
                return embedded
        if address.teredo is not None:
            return address.teredo[1]
    return address


def _is_global_address(raw_address: str) -> bool:
    """True only for a globally routable address.

    `is_global` covers loopback, private, link-local (including the 169.254.169.254
    metadata address), reserved, multicast, and unspecified in one check, for v4 and
    v6 alike.
    """
    try:
        address = ipaddress.ip_address(raw_address.split("%", 1)[0])
    except ValueError:
        return False
    return bool(_unwrap(address).is_global)


def _reject(canonical: str, reason: UrlRejectReason) -> UrlVerdict:
    return UrlVerdict(allowed=False, canonical_url=canonical[:_MAX_URL_LEN], reason=reason)


async def evaluate_url(raw_url: str, *, resolve_dns: bool = True) -> UrlVerdict:
    """Decide whether one candidate URL may be handed to a page reader.

    `resolve_dns=False` skips only the final DNS check; every structural check still
    runs. Use it when the caller has already resolved the host or is re-checking a
    post-redirect URL the provider has demonstrably already reached.
    """
    url = (raw_url or "").strip()
    canonical = canonicalize(url)

    if not url:
        return _reject(canonical, UrlRejectReason.MALFORMED)
    if len(url) > _MAX_URL_LEN:
        return _reject(canonical, UrlRejectReason.TOO_LONG)

    try:
        parts = urlsplit(url)
        # .port validates the port even when we do not use the value.
        _ = parts.port
    except ValueError:
        return _reject(canonical, UrlRejectReason.MALFORMED)

    if parts.scheme.lower() != "https":
        return _reject(canonical, UrlRejectReason.NOT_HTTPS)
    if parts.username or parts.password:
        return _reject(canonical, UrlRejectReason.CREDENTIALS_IN_URL)

    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return _reject(canonical, UrlRejectReason.NO_HOST)

    # IP literals skip the DNS step entirely: the address is right there.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _unwrap(literal).is_global:
            return _reject(canonical, UrlRejectReason.PRIVATE_ADDRESS)
        return UrlVerdict(allowed=True, canonical_url=canonical)

    if host in _METADATA_HOSTS:
        return _reject(canonical, UrlRejectReason.METADATA_HOST)
    if host.endswith(_RESERVED_SUFFIXES):
        return _reject(canonical, UrlRejectReason.RESERVED_SUFFIX)
    if "." not in host:
        # A single-label name only resolves inside a private search domain.
        return _reject(canonical, UrlRejectReason.SINGLE_LABEL_HOST)

    query_keys = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if query_keys & _SIGNED_QUERY_KEYS:
        return _reject(canonical, UrlRejectReason.SIGNED_URL)

    if not resolve_dns:
        return UrlVerdict(allowed=True, canonical_url=canonical)

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        # A name we cannot resolve is refused, not passed through. An unresolvable
        # source becomes a recorded gap, which is the correct product behavior.
        return _reject(canonical, UrlRejectReason.DNS_FAILED)

    addresses = [str(info[4][0]) for info in infos if info[4]]
    if not addresses:
        return _reject(canonical, UrlRejectReason.DNS_FAILED)
    # EVERY resolved address must be global. One private answer in a round-robin set
    # is enough to refuse the host.
    if not all(_is_global_address(address) for address in addresses):
        return _reject(canonical, UrlRejectReason.PRIVATE_ADDRESS)

    return UrlVerdict(allowed=True, canonical_url=canonical)
