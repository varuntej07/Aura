"""Versioned, source-evidenced product knowledge for Buddy."""

from .catalog import (
    PRODUCT_KNOWLEDGE,
    CurrentProductSurface,
    ProductInfoKind,
    ProductKnowledgeResult,
    ProductTargetPlatform,
    ProductTargetSurface,
    lookup_product_knowledge,
)
from .digest import voice_capability_digest

__all__ = [
    "PRODUCT_KNOWLEDGE",
    "CurrentProductSurface",
    "ProductInfoKind",
    "ProductKnowledgeResult",
    "ProductTargetPlatform",
    "ProductTargetSurface",
    "lookup_product_knowledge",
    "voice_capability_digest",
]
