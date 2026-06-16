"""Topic tracking — the user-requested "keep me posted on X" engine.

A generic LLM agent researches ANY topic, derives its own lifespan + cadence,
and materializes checkpoints (pre/live/post) that fire live updates through a
cost-ordered fetch chain and a global notification gatekeeper.
"""
