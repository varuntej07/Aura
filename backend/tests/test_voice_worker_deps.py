"""
Regression guard for voice-worker dependency drift.

The voice worker (src/agent/voice_agent.py) imports plugins from livekit.plugins.
Each of those plugins ships as a separate package installed via a livekit-agents
EXTRA in pyproject.toml (e.g. livekit-agents[openai] -> livekit-plugins-openai).

If a plugin is imported but its extra is not declared, the code still runs
locally (the plugin happens to be in the dev venv) but the Docker image built
from pyproject.toml omits it, and the worker crashes at import with
"ImportError: cannot import name '<plugin>' from 'livekit.plugins'" — failing
the Cloud Run deploy after a ~5min build. This test catches that drift locally.

History: commit 3bc857d added the `openai` plugin import without adding the
`openai` extra; the worker deploy (revision 00031) crash-looped on startup.
"""

from __future__ import annotations

import re
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_VOICE_AGENT = _BACKEND_ROOT / "src" / "agent" / "voice_agent.py"
_PYPROJECT = _BACKEND_ROOT / "pyproject.toml"


def _plugins_imported_by_worker() -> set[str]:
    """Plugin module names referenced from livekit.plugins in voice_agent.py.

    Catches both `from livekit.plugins import a, b` and `livekit.plugins.x.y`.
    Normalizes module names to extra names (underscores -> hyphens).
    """
    source = _VOICE_AGENT.read_text(encoding="utf-8")
    plugins: set[str] = set()

    for match in re.finditer(r"from livekit\.plugins import ([^\n]+)", source):
        for name in match.group(1).split(","):
            cleaned = name.strip().split(" as ")[0].strip()
            if cleaned:
                plugins.add(cleaned)

    for match in re.finditer(r"from livekit\.plugins\.([a-z_]+)", source):
        plugins.add(match.group(1))

    return {p.replace("_", "-") for p in plugins}


def _declared_livekit_extras() -> set[str]:
    """Extras declared on the livekit-agents requirement in pyproject.toml."""
    text = _PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r"livekit-agents\[([^\]]+)\]", text)
    assert match, "livekit-agents requirement with extras not found in pyproject.toml"
    return {e.strip().replace("_", "-") for e in match.group(1).split(",")}


def test_every_imported_livekit_plugin_has_a_declared_extra():
    imported = _plugins_imported_by_worker()
    declared = _declared_livekit_extras()
    missing = imported - declared
    assert not missing, (
        f"Voice worker imports {sorted(missing)} from livekit.plugins but "
        f"pyproject.toml does not declare the matching livekit-agents extra(s). "
        f"Add them to the livekit-agents[...] requirement or the Docker image "
        f"will crash at startup. Declared: {sorted(declared)}"
    )
