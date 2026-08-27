# Buddy Keyboard Architecture

**This document has moved.** The canonical, current design lives at
[`backend/docs/BUDDY_KEYBOARD_ARCHITECTURE.md`](../backend/docs/BUDDY_KEYBOARD_ARCHITECTURE.md).
Read that before changing anything in
`android/app/src/main/kotlin/dev/varuntej/aura/keyboard/`.

The long version that used to live here described an architecture the keyboard no longer has. It
is removed rather than left in place because an indexed document that reads as current is worse
than no document: the next agent acts on it. Specifically, it described a plaintext
`SqlitePersonalDictionary` (personalization is now an AES-GCM snapshot under an Android Keystore
key, see `EncryptedPersonalizationStore`), a 30,000-line `en_wordlist.txt` `PrefixIndex` (the word
list is no longer shipped; `BaseDictionary` memory-maps `en_us.pdict`), and a neural decoder as
future work (an ONNX reranker tier now ships).

## What is not in the first beta

Two capabilities the old document listed under "the moat" are **not** active:

- **Cloud vocabulary hints.** `VocabHintsCache` and `ProperNounIndex` exist in the tree but
  `BuddyImeService` references neither, and `android/tools/keyboard_audit/audit_ime_scope.py`
  asserts that absence as a pass condition. Nothing fetches `GET /keyboard/vocab`.
- **Proper-noun casing from Aura vocabulary** (the "kcr" to "KCR" behaviour), which depended on
  the above.

The endpoint remains live in production with no shipping consumer. Do not describe either
capability as user-visible until something wires it back up within the privacy contract.

## Privacy contract

The rules that govern what may leave the device, and the consent required before it does, are in
[`backend/docs/keyboard-privacy-and-data-safety.md`](../backend/docs/keyboard-privacy-and-data-safety.md).
That document is the one the Privacy Policy, the Play Data Safety form, and the in-product strings
all have to agree with.
