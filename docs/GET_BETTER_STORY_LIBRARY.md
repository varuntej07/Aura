# Get Better story library

## Current behavior

Get Better is a reviewed, versioned story catalog. Every new user starts with
the same catalog so the experience is useful before Aura has enough evidence
to personalize it.

No LLM runs when the screen opens. The backend serves the catalog from process
memory and checks one Firestore metadata document at most once every 15 minutes
per backend process. Story documents are read only after the metadata version
changes.

The Flutter client stores the full catalog as one JSON snapshot in Drift. A
fresh snapshot renders without a network request. After the rolling 24-hour
revalidation interval, the client sends its known version in one request. The
backend returns a small `not_modified` response when nothing changed.

The 24-hour interval is cache revalidation only. It does not rotate, reorder,
generate, or assign stories.

## Publishing

The reviewed source is:

`backend/src/services/get_better/content/stories_v1.json`

Validate and preview a publish:

```text
cd backend
python scripts/publish_get_better_catalog.py
```

Apply only after reviewing the dry-run output:

```text
python scripts/publish_get_better_catalog.py --apply
```

The publisher writes versioned story documents first and changes the metadata
pointer last. Runtime readers therefore see either the complete old catalog or
the complete new catalog.

## User activity

Save, completion, open, related-open, share, and Buddy-chat events are written
to a durable local Drift outbox. The outbox flushes up to 50 events as one
idempotent backend batch. A successful flush costs one Firestore write and no
preliminary Firestore read. Failed batches remain on disk for retry.

Activity batches expire after 180 days through the
`get_better_activity.expires_at` Firestore TTL policy.

## Deferred personalization TODO

Do not add daily 4 AM rotation or per-user catalog generation yet.

When product behavior is decided, introduce learned ranking behind a separate
ranking contract. It should rank canonical story IDs, not generate story copy.
Keep the reviewed catalog as the fallback and reserve a small exploration
portion so ranking does not become repetitive. Train and evaluate from the
batched activity stream only after consent, minimum-data, privacy, cold-start,
and offline behavior are specified.
