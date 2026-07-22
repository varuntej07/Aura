"""Resolve delivery surfaces from user capability and notification behavior."""

from __future__ import annotations

from dataclasses import replace

from ...lib.logger import logger
from . import desktop_preferences
from .proposal import (
    SOURCE_BILLING,
    SOURCE_DEVICE_LINK,
    SOURCE_MEETING,
    SOURCE_TRIAL,
    SOURCE_WELCOME,
    DeliveryChannel,
    NotificationProposal,
    ProposalKind,
)

_ACCOUNT_SOURCES = {SOURCE_BILLING, SOURCE_TRIAL, SOURCE_WELCOME}


async def resolve(proposal: NotificationProposal) -> NotificationProposal:
    """Return a copy with the currently eligible surfaces.

    Preference reads fail closed to mobile-only. Meeting lifecycle events retain
    their explicit desktop-only contract; device-link alerts remain mobile-only
    so pairing a desktop does not notify that same desktop about itself.
    """
    mobile_only = frozenset({DeliveryChannel.MOBILE})
    if proposal.source == SOURCE_MEETING or proposal.channels != mobile_only:
        return proposal
    if proposal.source == SOURCE_DEVICE_LINK:
        return replace(proposal, channels=mobile_only)
    channels = {DeliveryChannel.MOBILE}
    try:
        preferences = await desktop_preferences.get(proposal.user_id)
        category_enabled = (
            preferences.proactive_enabled
            if proposal.kind == ProposalKind.PROACTIVE
            else preferences.account_enabled
            if proposal.source in _ACCOUNT_SOURCES
            else preferences.committed_enabled
        )
        if preferences.enabled and category_enabled:
            channels.add(DeliveryChannel.DESKTOP)
    except Exception as exc:
        logger.warn("desktop channel preference lookup failed", {
            "user_id": proposal.user_id,
            "source": proposal.source,
            "error_type": type(exc).__name__,
        })
    return replace(proposal, channels=frozenset(channels))
