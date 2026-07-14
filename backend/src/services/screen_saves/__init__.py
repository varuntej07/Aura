"""Screen Saves: durable, per-user bookmarks of a screen-sight frame the user
explicitly asked Buddy to remember (see ``architectures/desktop-buddy.md``).

See ``fields.py`` for the document contract, ``collections.py`` for the
free-form collection-name resolver (semantic dedup via ``find_nearest``,
mirroring ``services/memory/retrieval.py``), and ``store.py`` for the item
writer/reader shared by the voice tool and the REST handlers.
"""
