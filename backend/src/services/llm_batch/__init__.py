"""Durable, provider-agnostic queue for asynchronous batch LLM work.

Batch inference is asynchronous by nature — the Gemini Batch API targets a 24h
turnaround, expires jobs at 48h, and bills at 50% of interactive rates. Nothing
can await that inside a request, so callers enqueue items, a scheduler-driven
poller advances the jobs, and results are dispatched to a handler registered
against the item's ``job_kind``.

This package is deliberately NOT specific to any one feature: ``job_kind`` is the
only coupling, so a new consumer registers a handler and enqueues items without
touching anything here.
"""
