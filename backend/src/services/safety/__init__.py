"""User-safety primitives: age banding and age-signal capture.

Deliberately separate from ``entitlement``. Entitlement answers "what has this
user paid for"; this package answers "what does this user's own account say
about their age, and what have they told us that contradicts it". Those two
questions have different failure directions and must never share a module: an
entitlement outage should degrade to the free product, while an age question
that cannot be answered must degrade to UNKNOWN and never to ADULT.
"""
