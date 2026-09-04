"""Dynamic voice context appended after a stable surface-specific prompt."""

from __future__ import annotations

VOICE_SESSION_CONTEXT_START = "<session>"

# Spoken-friendly device names for the prompt. An unrecognized or missing
# platform renders NOTHING rather than a guess: the boundary test builds agents
# without these keys and its rendered output must stay byte-identical.
_PLATFORM_LABELS = {
    "android": "an Android phone",
    "ios": "an iPhone",
    "windows": "a Windows PC",
    "macos": "a Mac",
}


def _device_line(context_vars: dict[str, str]) -> str:
    label = _PLATFORM_LABELS.get(context_vars.get("client_platform", ""), "")
    if not label:
        return ""
    version = context_vars.get("app_version", "")
    suffix = f", Aura version {version}" if version else ""
    return f"\n            Device: {label}{suffix}"


def render_voice_session_context(context_vars: dict[str, str]) -> str:
    """Render every per-session value once, after all stable instructions."""
    return f"""
            {VOICE_SESSION_CONTEXT_START}
            Background. Latest finalized user turn has authority.
            User: {context_vars["name"]}
            Local: {context_vars["local_time"]}, {context_vars["local_date"]},
            {context_vars["timezone"]}{_device_line(context_vars)}
            Archive:
            {context_vars["archive_context"]}
            Aura profile:
            {context_vars["user_aura_profile"]}
            Last session:
            {context_vars["last_session_context"]}
            Recent memory:
            {context_vars["memory_summary"]}{context_vars["graph_context"]}{context_vars.get("text_chat_context", "")}
            </session>
"""
