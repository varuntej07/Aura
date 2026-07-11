"""Data providers, one module per source.

Each provider returns plain JSON-able dicts/lists and never raises into the request:
a source that is down or misconfigured returns an empty result and logs loudly, so one
broken panel never takes down the whole dashboard.
"""
