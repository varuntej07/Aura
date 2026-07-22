from src.services.notifications import channel_policy, desktop_preferences
from src.services.notifications.proposal import (
    SOURCE_DEVICE_LINK,
    SOURCE_MEETING,
    SOURCE_NEWS,
    SOURCE_REMINDER,
    DeliveryChannel,
    NotificationProposal,
    ProposalKind,
)


def _proposal(source: str, kind: ProposalKind) -> NotificationProposal:
    return NotificationProposal(
        user_id="user-1",
        source=source,
        kind=kind,
        dedup_key=f"{source}:1",
    )


async def test_enabled_desktop_receives_committed_and_proactive(monkeypatch):
    async def enabled(user_id: str):
        return desktop_preferences.DesktopPreferences(enabled=True)

    monkeypatch.setattr(channel_policy.desktop_preferences, "get", enabled)

    for proposal in (
        _proposal(SOURCE_REMINDER, ProposalKind.COMMITTED),
        _proposal(SOURCE_NEWS, ProposalKind.PROACTIVE),
    ):
        resolved = await channel_policy.resolve(proposal)
        assert resolved.channels == frozenset(
            {DeliveryChannel.MOBILE, DeliveryChannel.DESKTOP}
        )


async def test_disabled_desktop_fails_closed_to_mobile(monkeypatch):
    async def disabled(user_id: str):
        return desktop_preferences.DesktopPreferences(enabled=False)

    monkeypatch.setattr(channel_policy.desktop_preferences, "get", disabled)
    resolved = await channel_policy.resolve(
        _proposal(SOURCE_REMINDER, ProposalKind.COMMITTED)
    )
    assert resolved.channels == frozenset({DeliveryChannel.MOBILE})


async def test_security_and_meeting_sources_keep_explicit_surface_contracts():
    device = await channel_policy.resolve(
        _proposal(SOURCE_DEVICE_LINK, ProposalKind.COMMITTED)
    )
    assert device.channels == frozenset({DeliveryChannel.MOBILE})

    meeting = _proposal(SOURCE_MEETING, ProposalKind.COMMITTED)
    meeting.channels = frozenset({DeliveryChannel.DESKTOP})
    resolved = await channel_policy.resolve(meeting)
    assert resolved.channels == frozenset({DeliveryChannel.DESKTOP})
