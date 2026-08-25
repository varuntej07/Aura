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

__all__ = [
    "PRODUCT_KNOWLEDGE",
    "CurrentProductSurface",
    "ProductInfoKind",
    "ProductKnowledgeResult",
    "ProductTargetPlatform",
    "ProductTargetSurface",
    "lookup_product_knowledge",
]
