"""Daily Briefing engine.

One synthesized, Buddy-voice morning digest per user, woven from the content pool
the signal engine already ranks.

  briefing_engine.run_briefing_tick — fan-out, rides the scheduler tick.
  briefing_agent.generate          — the "middle man": rank, judge, synthesize.
  briefing_store                    — transactional once-per-day claim + read/write.
  fields                            — Firestore field-name single source of truth.
"""
