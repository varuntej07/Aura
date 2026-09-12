"""One structural rule Firestore enforces at commit time, checked before the commit.

Firestore refuses an array that holds another array as a DIRECT element. An array
inside a map inside an array is fine, which is why the plan's ``sub_questions`` (each
a map carrying ``entity_bindings``) commits happily while ``effective_policy``'s
conjunction of disjunctions did not.

Why this exists as a check rather than just a fixed encoding: a rejected commit raises
from ``store.advance``, which is OUTSIDE the handler in ``engine.advance`` that turns a
stage failure into a recorded failure code. The write error therefore escaped as a bare
HTTP 500 with nothing written to the run, and the run sat in ``planning`` for eleven
minutes before the attempt cap noticed. Raising from inside the stage body instead
routes through the normal failure path, so the same mistake made later is attributed,
bounded and visible rather than silent.

Deliberately ONE rule. This is not a Firestore validator: document size, field-name
legality, nesting depth and the rest are still the database's business. It checks the
single constraint that a plain ``model_dump`` of a nested tuple walks straight into.
"""

from __future__ import annotations

from typing import Any


class NestedArrayError(ValueError):
    """A list was found as a direct element of another list."""


def assert_firestore_safe(value: Any, *, path: str = "") -> None:
    """Raise NestedArrayError if ``value`` holds an array directly inside an array.

    ``path`` names the offending field the way Firestore's own message does, so the
    log says which field to fix rather than which document failed.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            assert_firestore_safe(item, path=f"{path}.{key}" if path else str(key))
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            if isinstance(item, (list, tuple)):
                raise NestedArrayError(
                    f"array nested in array at {path or '<root>'}[{index}]"
                )
            assert_firestore_safe(item, path=f"{path}[{index}]")
