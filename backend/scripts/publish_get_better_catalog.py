"""Validate and publish the reviewed Get Better story catalog.

Dry-run is the default. From ``backend/``:

    python scripts/publish_get_better_catalog.py
    python scripts/publish_get_better_catalog.py --apply

Story documents are written before the single metadata pointer. If publishing
fails midway, readers continue using the previous version because the pointer
does not move.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.firebase import admin_firestore  # noqa: E402
from src.services.get_better.catalog import (  # noqa: E402
    CATALOG_METADATA_COLLECTION,
    CATALOG_METADATA_DOCUMENT,
    PACKAGED_CATALOG_PATH,
    STORY_COLLECTION,
)
from src.services.get_better.models import GetBetterCatalog  # noqa: E402


def load_catalog(path: Path) -> GetBetterCatalog:
    return GetBetterCatalog.model_validate_json(path.read_text(encoding="utf-8"))


def publish_catalog(
    catalog: GetBetterCatalog,
    *,
    apply: bool,
    force: bool,
) -> dict[str, Any]:
    database = admin_firestore()
    metadata_ref = (
        database.collection(CATALOG_METADATA_COLLECTION).document(CATALOG_METADATA_DOCUMENT)
    )
    current_snapshot = metadata_ref.get()
    current = current_snapshot.to_dict() or {} if current_snapshot.exists else {}
    current_version = current.get("catalog_version")
    if current_version == catalog.catalog_version and not force:
        return {
            "changed": False,
            "catalog_version": catalog.catalog_version,
            "stories": len(catalog.published_stories),
            "reason": "already_published",
        }

    result = {
        "changed": True,
        "from_version": current_version,
        "catalog_version": catalog.catalog_version,
        "stories": len(catalog.published_stories),
        "apply": apply,
    }
    if not apply:
        return result

    batch = database.batch()
    write_count = 0
    for story in catalog.stories:
        story_ref = database.collection(STORY_COLLECTION).document(story.id)
        batch.set(
            story_ref,
            {
                **story.model_dump(mode="json"),
                "catalog_version": catalog.catalog_version,
            },
        )
        write_count += 1
        if write_count == 450:
            batch.commit()
            batch = database.batch()
            write_count = 0
    if write_count:
        batch.commit()

    metadata_ref.set(
        {
            "catalog_version": catalog.catalog_version,
            "published_at": catalog.published_at,
            "headline": catalog.headline,
            "intro": catalog.intro,
            "story_ids": [story.id for story in catalog.published_stories],
        }
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=PACKAGED_CATALOG_PATH,
        help="path to the versioned catalog JSON",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write to Firestore; default is a read-only dry-run",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="republish the same version; requires --apply",
    )
    args = parser.parse_args()
    if args.force and not args.apply:
        parser.error("--force requires --apply")

    catalog = load_catalog(args.catalog)
    result = publish_catalog(catalog, apply=args.apply, force=args.force)
    print(json.dumps(result, indent=2, default=str))
    if not args.apply:
        print("Nothing was written. Re-run with --apply after reviewing this diff.")


if __name__ == "__main__":
    main()
