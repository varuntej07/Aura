"""Query-relevant long-term memory: an unbounded per-user atom store plus
intent-gated, semantic retrieval that surfaces the memories relevant to the
CURRENT message (not a static top-N).

See ``fields.py`` for the atom document contract, ``atom_store.py`` for the
writer, and ``retrieval.py`` for the reader.
"""
