"""Country resolution from trusted edge headers.

The mobile paywall must not decide purchase-link visibility from the device
locale (user-configurable, unrelated to any storefront), so GET /entitlement
serves a server-resolved country instead. Bare Cloud Run sets none of these
headers, in which case this returns None and clients stay in their always-legal
silent mode. Behind Google Cloud Load Balancer, add a custom request header
X-Client-Geo-Country: {client_region} and real values flow with zero code
change here.
"""

from __future__ import annotations

import re

from fastapi import Request

# Checked in order; the first plausible two-letter value wins. All of these are
# set by edges we would sit behind, never by end-user devices reaching Cloud
# Run directly through them.
_COUNTRY_HEADERS = (
    "x-client-geo-country",  # our GCLB custom header ({client_region})
    "x-country-code",
    "x-appengine-country",
    "cf-ipcountry",
    "x-vercel-ip-country",
)

_ISO_ALPHA2 = re.compile(r"^[A-Za-z]{2}$")

# Placeholder codes edges emit when they could not geolocate the client.
_UNKNOWN_CODES = ("ZZ", "XX", "T1")


def resolve_request_country(request: Request) -> str | None:
    """ISO 3166-1 alpha-2 country (uppercase) for the calling client, or None
    when no trusted edge header carries one."""
    for header in _COUNTRY_HEADERS:
        value = str(request.headers.get(header, "")).strip()
        if not _ISO_ALPHA2.match(value):
            continue
        code = value.upper()
        if code in _UNKNOWN_CODES:
            continue
        return code
    return None
