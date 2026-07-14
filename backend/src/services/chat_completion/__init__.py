"""Durable server-side completion of chat turns.

The live ``POST /chat`` SSE stream is coupled to the client connection: when the
phone backgrounds, Starlette cancels the generator and the answer is lost. This
package lets a turn finish server-side anyway and notifies the user when ready.

Pieces:
  prompt_builder.py  shared system-prompt construction (used by the live handler
                     AND the completion path, so both build the exact same prompt)
  turn_store.py      Firestore state for an in-flight turn (write/claim/complete)
  completion.py      the regenerate-or-synthesize logic the Cloud Task runs
"""
