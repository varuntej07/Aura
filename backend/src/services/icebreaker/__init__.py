"""The Icebreaker engine — life-aware conversation openers.

~3 random days a week (a deterministic weekly dice roll), Buddy sends ONE warm
opener at a random good local hour, built entirely from FREE context (the user's
region, today's weather, fresh headlines matched to their interests, and the
sparse life_facts learned passively from chat) and never repeating a past opener.

Mirrors the structure of ``services/threads``: a pure scheduler-logic module, a
Firestore store with an atomic claim, a context assembler, an LLM framer with a
reject gate, and a thin engine orchestrator ridden on the per-minute scheduler
tick.
"""
