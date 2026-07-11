"""Reactive orchestration layer (event bus + outbox + per-user brain).

This package replaces the cron-gated, isolated proactive producers with an
event-driven flow: producers emit a typed ``Event`` through the transactional
outbox, a relay publishes it (a coalesced ``/internal/orchestrate`` Cloud Task
per user), and the per-user orchestrator reconciles, decides, and dispatches
agents inside a self-heal envelope.

Built in safe phases (see the design doc). P0 is the bus + outbox + dispatch
plumbing; the orchestrator is a shadow no-op until P2 wires the brain.
"""
