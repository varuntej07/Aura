"""Mapping a real domain to a SourceClass. Learned and cached, never hardcoded.

There is no hostname allowlist anywhere in this package, and there must not be one. A
list of "trusted domains" is wrong within a month, encodes the author's assumptions
about which parts of the web matter, and silently fails for every domain nobody thought
of. Instead a cheap batched model call classifies domains by the ROLE they play, and the
answer is cached globally.

``research_domain_classes`` is a ROOT collection on purpose. A domain's source class is
not user data: stripe.com is a company's own site regardless of who searched for it, so
amortising the classification across every user is the entire point. The document stores
only registrable domain, class, confidence, votes and expiry. It never stores the user's
query, entity, page title, snippet, uid or run id, which is what makes the sharing safe.

After a few weeks the common web is classified and the marginal cost approaches zero,
while the mechanism stays learned, keeps refreshing on a 30-day TTL, and contains no
baked-in knowledge that can rot.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from google.cloud import firestore as gcloud_firestore

from ...lib.logger import logger
from ..firebase import admin_firestore
from ..model_provider import get_model_provider
from . import fields as F
from .llm_models import DomainClassBatch
from .policy_table import SourceClass
from .prompts import DOMAIN_CLASS_SYSTEM, domain_class_user_prompt

# One call classifies at most this many domains. Matches DomainClassBatch's bound.
BATCH_MAX = 25

# PRIMARY_ORG is a RUN-RELATIVE ROLE, not a property of the publisher, and it must never
# enter the shared cross-user cache.
#
# stripe.com is the primary source for a run about Stripe and vendor marketing for a run
# comparing payment processors. The cache is a root collection read by every user, so
# writing "stripe.com is primary_org" because ONE run's subject happened to be Stripe hands
# every later run, on every other subject, a source that satisfies primary_source_required
# on its own. The cache stores what a publisher IS; the run computes what it is TO THIS
# QUESTION.
#
# VENDOR_MARKETING is the context-independent identity behind primary_org: a company's own
# site. apply_primary_source_role promotes it per run when the run is actually about that
# company.
_UNCACHEABLE_CLASSES = {SourceClass.PRIMARY_ORG}
_CACHEABLE_SUBSTITUTE = SourceClass.VENDOR_MARKETING

# A single low-confidence guess neither qualifies for this run nor enters a cache every
# other user reads. Below this threshold the outcome is explicitly UNKNOWN.
MIN_CACHE_CONFIDENCE = 0.6


def registrable_domain(url: str) -> str:
    """The host, lowercased, with a leading www. removed. A CACHE KEY, nothing more.

    Deliberately NOT a public-suffix parse. Getting "co.uk" style suffixes exactly right
    needs a PSL dependency, and being wrong here costs a duplicated cache entry for a
    subdomain, not a correctness failure: the class of docs.stripe.com and stripe.com is
    nearly always the same, and when it is not, two entries is the honest answer anyway.

    Do NOT reuse this to count independent sources. That reasoning holds only because a
    duplicate cache entry is harmless, and it stops holding the moment the value is used
    to decide whether two pages corroborate each other. Use ``independence_key``.
    """
    try:
        host = (urlsplit(url).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# Second-level labels that are part of the SUFFIX rather than the registrable name, so
# "bbc.co.uk" is one publisher and not "co.uk". This is not the full Public Suffix List
# and is not trying to be: it covers the shape that actually recurs (a country code with
# a generic second level), and every case it gets wrong it gets wrong in the SAFE
# direction. See independence_key.
_SUFFIX_SECOND_LEVELS = frozenset({
    "co", "com", "net", "org", "gov", "edu", "ac", "mil", "govt", "gob", "or", "ne",
})


def independence_key(url_or_domain: str) -> str:
    """The PUBLISHER behind a URL. Two pages sharing this key corroborate nothing.

    ``registrable_domain`` strips only "www.", which is correct for a cache key and wrong
    here: it made ``a.contentfarm.com``, ``b.contentfarm.com`` and ``c.contentfarm.com``
    three independent publishers, so a policy demanding two corroborating domains was
    satisfied by one site with three subdomains.

    This collapses a host to eTLD+1 without a Public Suffix List dependency, by treating
    a two-letter final label plus a known generic second level as the suffix. That is
    exact for the common shapes (``bbc.co.uk``, ``abc.com.au``, ``x.ac.uk``) and
    approximate for hosting suffixes the PSL knows about and this does not, such as
    ``github.io`` or ``s3.amazonaws.com``, where two genuinely different publishers
    collapse to one key.

    That error direction is deliberate. Under-counting independence can only make
    corroboration HARDER to reach, never easier, so the failure mode is a claim reported
    as single-source that deserved better, not a claim reported as corroborated that was
    never corroborated at all. For a verification rule, being conservative when unsure is
    the only acceptable bias. Swap in a real PSL if the false merges ever matter more
    than the dependency.
    """
    host = url_or_domain
    if "//" in host or "/" in host:
        host = registrable_domain(url_or_domain)
    else:
        host = host.lower().strip(".")
        if host.startswith("www."):
            host = host[4:]
    if not host or ":" in host:
        return host
    labels = host.split(".")
    if len(labels) < 3:
        return host
    # An IP literal has no registrable domain; the host IS the publisher.
    if all(label.isdigit() for label in labels):
        return host
    if len(labels[-1]) == 2 and labels[-2] in _SUFFIX_SECOND_LEVELS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _doc_id(domain: str) -> str:
    return hashlib.sha256(domain.encode("utf-8")).hexdigest()[:24]


def _collection() -> Any:
    return admin_firestore().collection(F.DOMAIN_CLASS_COLLECTION)


async def _read_cached(domains: list[str]) -> dict[str, SourceClass]:
    """Fetch whatever is already classified. A cache miss is never an error."""
    if not domains:
        return {}

    def _run() -> dict[str, SourceClass]:
        found: dict[str, SourceClass] = {}
        collection = _collection()
        now_iso = datetime.now(UTC).isoformat()
        for domain in domains:
            try:
                snap = collection.document(_doc_id(domain)).get()
            except Exception:
                continue
            if not snap.exists:
                continue
            data = snap.to_dict() or {}
            # An expired row is treated as a miss even before TTL physically deletes it.
            # TTL deletion is asynchronous and can lag by days; trusting a stale class
            # that long would defeat the point of having an expiry at all.
            if str(data.get(F.EXPIRES_AT, "")) <= now_iso:
                continue
            if float(data.get("confidence", 0.0)) < MIN_CACHE_CONFIDENCE:
                found[domain] = SourceClass.UNKNOWN
                continue
            raw = str(data.get("source_class", ""))
            if raw in SourceClass.__members__.values() or raw in {c.value for c in SourceClass}:
                found[domain] = SourceClass(raw)
        return found

    return await asyncio.to_thread(_run)


async def _write_cached(entries: list[dict[str, Any]]) -> None:
    """Persist newly classified domains. Best effort: a failed write is a cache miss."""
    if not entries:
        return
    expires_at = (datetime.now(UTC) + timedelta(days=F.DOMAIN_CLASS_TTL_DAYS)).isoformat()
    now_iso = datetime.now(UTC).isoformat()

    def _run() -> None:
        collection = _collection()
        for entry in entries:
            domain = str(entry.get("domain", ""))
            if not domain:
                continue
            try:
                collection.document(_doc_id(domain)).set(
                    {
                        "domain": domain,
                        "source_class": str(entry.get("source_class", "")),
                        "confidence": float(entry.get("confidence", 0.0)),
                        # Votes make a later refresh able to see how often this domain
                        # has been re-derived, without storing who asked.
                        "votes": gcloud_firestore.Increment(1),
                        F.EXPIRES_AT: expires_at,
                        F.UPDATED_AT: now_iso,
                    },
                    merge=True,
                )
            except Exception as exc:
                logger.warn(
                    "research.domain_class: cache write failed",
                    {"error": str(exc), "error_code": "research_domain_cache_write_failed"},
                )

    await asyncio.to_thread(_run)


async def classify_domains(
    rows: list[dict[str, str]], *, max_output_tokens: int | None = None
) -> dict[str, SourceClass]:
    """Resolve a SourceClass for every domain in ``rows``. Cache first, then one call.

    ``rows`` carries domain, title and snippet. Page BODY is deliberately absent: letting
    a page's content influence its own trust rating would invert the whole point of
    classifying the publisher. Titles and snippets come from Brave, not from us following
    the link, so nothing here requires having read the page.

    Never raises. A provider failure returns UNKNOWN for every unclassified domain, so
    callers cannot accidentally supply a trusted default.
    """
    by_domain: dict[str, dict[str, str]] = {}
    for row in rows:
        domain = row.get("domain", "")
        if domain and domain not in by_domain:
            by_domain[domain] = row

    resolved = await _read_cached(list(by_domain))
    missing = [row for domain, row in by_domain.items() if domain not in resolved]
    if not missing:
        return resolved

    provider = get_model_provider()
    written: list[dict[str, Any]] = []
    # Chunked so a wide wave cannot exceed the schema bound or build one huge prompt.
    for start in range(0, len(missing), BATCH_MAX):
        chunk = missing[start : start + BATCH_MAX]
        try:
            result = await provider.cheap(
                domain_class_user_prompt(chunk),
                system=DOMAIN_CLASS_SYSTEM,
                response_model=DomainClassBatch,
                max_output_tokens=max_output_tokens,
            )
        except Exception as exc:
            logger.warn(
                "research.domain_class: classification failed",
                {
                    "error": str(exc),
                    "count": len(chunk),
                    "error_code": "research_domain_classify_failed",
                },
            )
            continue
        if not isinstance(result, DomainClassBatch):
            continue
        for item in result.domains:
            domain = registrable_domain(f"https://{item.domain}") or item.domain
            if domain not in by_domain:
                # The model answered about a domain we did not ask about. Dropped rather
                # than cached: a hallucinated domain must not enter a shared cache that
                # every other user reads.
                continue
            # The identity that may be SHARED. A run-relative role is collapsed to the
            # context-independent publisher type behind it before it goes anywhere near
            # the cache or the caller; the run computes the role itself.
            identity = (
                _CACHEABLE_SUBSTITUTE
                if item.source_class in _UNCACHEABLE_CLASSES
                else item.source_class
            )
            if item.confidence < MIN_CACHE_CONFIDENCE:
                # Neither used as trusted for this run nor persisted for everyone else's.
                # An unconfident class is an UNKNOWN publisher, not a weak promotion.
                logger.info(
                    "research.domain_class: low confidence, not cached",
                    {"confidence": item.confidence, "threshold": MIN_CACHE_CONFIDENCE},
                )
                resolved[domain] = SourceClass.UNKNOWN
                continue
            resolved[domain] = identity
            written.append(
                {
                    "domain": domain,
                    "source_class": identity.value,
                    "confidence": item.confidence,
                }
            )

    for domain in by_domain:
        resolved.setdefault(domain, SourceClass.UNKNOWN)
    await _write_cached(written)
    return resolved


def apply_primary_source_role(
    classes: dict[str, SourceClass], entities: list[str]
) -> dict[str, SourceClass]:
    """Promote a domain to PRIMARY_ORG for THIS RUN when it is that entity's own site.

    Run-relative and never cached: the global cache stores what a publisher is, and this
    decides what it is to this particular question. stripe.com is the primary source for a
    run about Stripe and vendor marketing for a run comparing payment processors, and only
    the run knows which.

    Promotion requires EXACT normalized organization identity. The registrable name must
    equal the entity's own compacted form. Nothing else qualifies, because PRIMARY_ORG is
    the highest-trust class there is and satisfies ``primary_source_required`` on its own.
    """
    if not entities:
        return classes
    allowed = _entity_identity_forms(entities)
    if not allowed:
        return classes
    out = dict(classes)
    for domain, source_class in classes.items():
        if source_class is not SourceClass.VENDOR_MARKETING:
            continue
        # The registrable name only, so a TLD is never the thing that matched.
        name = independence_key(domain).split(".")[0].casefold()
        if name and name in allowed:
            out[domain] = SourceClass.PRIMARY_ORG
    return out


# Kept as an alias so the older name still resolves; the behaviour is the strict one.
apply_entity_override = apply_primary_source_role


def _entity_identity_forms(entities: list[str]) -> set[str]:
    """The exact strings a domain's registrable name may EQUAL to be that organization.

    Two rounds of this have now been wrong in the same direction, and the second is the
    one being fixed here.

    Round one matched by bidirectional containment, so for the entity "OpenAI" the domain
    ``ai.com`` (registrable name ``ai``) matched because "ai" is in "openai".

    Round two replaced that with equality against the compacted entity OR against each
    word of four-plus characters. The word path is the same laundering primitive wearing a
    length floor: the entity "Stripe Company" emits {stripecompany, stripe, company}, so
    ``company.com`` - an unrelated domain, plausibly an aggregator - is promoted to the
    highest-trust class there is. Any generic noun in an entity name donates its trust to
    whoever owns the matching domain. "Delta Air Lines" hands ``lines.com`` primary status;
    "Apple Music" hands it to ``music.com``.

    A domain now matches only the WHOLE entity, compacted, plus the whole entity with a
    trailing legal suffix removed. "Open AI" still matches ``openai.com`` and "Stripe Inc"
    still matches ``stripe.com``, because those are the same organization written two ways.
    A single generic word of a multi-word name is not that organization and never was.
    """
    forms: set[str] = set()
    for raw in entities:
        entity = raw.casefold().strip()
        if not entity:
            continue
        words = [word for word in entity.replace(",", " ").split() if word]
        # Drop a trailing legal-form suffix, which is punctuation about the company rather
        # than part of its name. Only ONE, and only at the end, so "Inc Magazine" keeps it.
        if len(words) > 1 and _strip_alnum(words[-1]) in _LEGAL_SUFFIXES:
            words = words[:-1]
        compact = "".join(_strip_alnum(word) for word in words)
        if compact:
            forms.add(compact)
    return forms


def _strip_alnum(word: str) -> str:
    return "".join(ch for ch in word if ch.isalnum())


# Legal forms, not name words. Removing one keeps "Stripe Inc" and "Stripe" the same
# organization without letting any other word of a name stand in for the whole.
_LEGAL_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "plc", "corp", "corporation",
    "co", "gmbh", "ag", "sa", "nv", "bv", "ab", "oy", "as", "pty", "srl", "spa",
    "kk", "pte", "llp", "lp", "group", "holdings",
})
