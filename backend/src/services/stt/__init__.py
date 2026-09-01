"""Speech-to-text provider access owned in one place.

Today this package owns Deepgram credential minting (``deepgram_grant``),
which was previously duplicated near-verbatim in the dictation and Interview
Companion handlers. Meeting prerecorded transcription still lives in
``services/meetings/`` and is a candidate to move here when it grows a second
consumer.
"""
