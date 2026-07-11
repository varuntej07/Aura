"""Shared "new device linked" security push.

Used by both the mobile-pairing claim flow (``handlers/pairing.py``) and the
web-auth signup flow (``handlers/web_auth.py``) so the copy and dedup-key shape
can never drift between the two callers.
"""

from __future__ import annotations

from . import orchestrator
from .proposal import SOURCE_DEVICE_LINK, NotificationProposal, ProposalKind


async def send_new_device_linked_push(
    user_id: str, device_name: str, linked_device_doc_id: str
) -> None:
    """Confirmation push through the notification funnel: COMMITTED lane, so it
    sends inline on submit (freshness + dedup only, never held or arbitrated).
    Leads with the good news (a device just got linked) rather than a
    security-alert tone, while keeping the same unlink escape hatch."""
    proposal = NotificationProposal(
        user_id=user_id,
        source=SOURCE_DEVICE_LINK,
        kind=ProposalKind.COMMITTED,
        dedup_key=f"device_link:{linked_device_doc_id}",
        title="Desktop connected",
        body=(
            f"Buddy's now on your Windows PC ('{device_name}'). "
            "Didn't do this? Unlink it in Settings."
        ),
    )
    await orchestrator.submit(proposal)
