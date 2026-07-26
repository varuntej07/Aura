# User Aura and memory architecture

Aura builds a consent-gated user model in two tiers: fast per-turn capture and slower whole-session reflection. User-facing memory is stored as typed atoms that can be listed and deleted.

## Component and data flow

```text
+---------------- user conversation ----------------+
| text turn | thread reply | completed session      |
+---------------------+------------------------------+
                      |
          +-----------+--------------------+
          |                                |
          v                                v
+----------------------+        +-------------------------+
| per-turn extractor   |        | session consolidation   |
| explicit facts,      |        | full transcript arc     |
| interests, goals     |        +------------+------------+
+----------+-----------+                     |
           |                         reflection patch
           +----------------+----------------+
                            v
                 +--------------------------+
                 | consent-gated transaction|
                 | merge, dedup, prune      |
                 +------------+-------------+
                              |
             +----------------+----------------+
             v                                 v
  +----------------------+          +----------------------+
  | UserAura profile     |          | typed memory atoms   |
  | interests/storylines |          | facts/traits/etc.    |
  +----------+-----------+          +----------+-----------+
             +----------------+----------------+
                              v
                    chat/voice personalization
```

Fresh users have no stored Aura, so prompts use neutral defaults. Returning users get consent-allowed summaries. Users may view and delete stored atoms even after revoking processing consent.

## Failure, retry, and recovery

```text
Consent absent/revoked ----------> skip capture and personalization
Per-turn model extraction fails -> swallow/log; user response is unaffected
Session has fewer than 2 turns --> skip reflection as low-value
Duplicate session submission ----> reflected-session count makes it idempotent
Concurrent capture/reflection ---> model call outside lock; short transaction merges patch
Long session --------------------> cheap map compression, then balanced reflection
Profile read in chat/voice fails -> empty context; conversation still starts
User deletes an atom ------------> hard delete; future explicit restatement may recreate it
```

## Obvious walkthrough: remember a durable preference

1. The user explicitly says they dislike early-morning workouts.
2. Per-turn extraction identifies a durable first-person preference.
3. A consent-gated merge stores it without blocking the chat response.
4. A later chat or voice session can include the summary in its prompt.

## Non-obvious walkthrough: correction during a long session

1. Early capture incorrectly records a relocation destination as the user's home city.
2. At session end, the client posts the transcript for consolidation.
3. Reflection sees the full arc and emits a life-fact correction plus canonical lists.
4. A short Firestore transaction re-reads the latest profile and applies only reflection-owned changes, preserving concurrent turn captures.
5. A retry with the same session turn count becomes a no-op.

## Code anchors

- `backend/src/services/user_aura_extractor.py`
- `backend/src/services/aura_reflection.py`
- `backend/src/services/user_aura_schema.py`
- `backend/src/services/memory/`
- `backend/src/handlers/aura.py`
