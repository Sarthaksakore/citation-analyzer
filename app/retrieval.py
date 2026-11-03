"""
app/retrieval.py — Layers 1 & 2: Researcher identification + multi-source citation retrieval.

Layer 1 public API
------------------
  resolve_researcher(query)              — name / ORCID / DOI → top-3 candidates
  get_researcher_publications(author_id) — all works for an OpenAlex author ID

Layer 2 public API
------------------
  get_citing_papers(cited_work_id, cited_doi) — normalized citing records from all sources
  get_all_citations_for_researcher(openalex_author_id) — full raw citation list
  get_ieee_scopus_wos_citations()        — stub; requires institutional API keys

Sources
-------
  OpenAlex          — filter=cites:{work_id}   (free, cursor-paginated)
  Semantic Scholar  — /paper/{id}/citations    (free; key optional)
  IEEE / Scopus / WoS — stub; institutional access required (TODO)

HTTP layer
----------
  _get_with_retry() — exponential backoff on 429/503; polite User-Agent on every call
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from app.schemas import PublicationRecord, RawCitationRecord, ResearcherProfile, SourceDatabase

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

OPENALEX_EMAIL: str = os.getenv("OPENALEX_EMAIL", "")
if OPENALEX_EMAIL.lower() in {"your@email.com", "", "none"}:
    OPENALEX_EMAIL = ""
SEMANTIC_SCHOLAR_API_KEY: str = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
ENABLE_S2_UNCAPPED: bool = os.getenv("ENABLE_S2_UNCAPPED", "false").lower() in {"1", "true", "yes"}
ENRICH_CROSSREF_UNCAPPED: bool = os.getenv("ENRICH_CROSSREF_UNCAPPED", "false").lower() in {"1", "true", "yes"}

OPENALEX_BASE = "https://api.openalex.org"
S2_BASE = "https://api.semanticscholar.org/graph/v1"
CROSSREF_BASE = "https://api.crossref.org"
OPENCITATIONS_BASE = "https://opencitations.net/index/coci/api/v1"

# Polite base delay between requests (seconds)
REQUEST_DELAY: float = 0.5

# Keep external requests bounded while allowing independent works and sources
# to be retrieved concurrently.
RETRIEVAL_WORKERS: int = max(1, int(os.getenv("RETRIEVAL_WORKERS", "8")))
S2_REQUEST_WORKERS: int = max(1, int(os.getenv("S2_REQUEST_WORKERS", "1")))
CROSSREF_REQUEST_WORKERS: int = max(1, int(os.getenv("CROSSREF_REQUEST_WORKERS", "2")))
_S2_REQUEST_GATE = threading.Semaphore(S2_REQUEST_WORKERS)
_CROSSREF_REQUEST_GATE = threading.Semaphore(CROSSREF_REQUEST_WORKERS)

# Retry configuration for transient errors
MAX_RETRIES: int = 4
BACKOFF_BASE: float = 2.0  # seconds; doubles each attempt

# Polite User-Agent sent on every outbound request
_contact = OPENALEX_EMAIL or "citation-analyzer"
POLITE_USER_AGENT: str = f"citation-analyzer/0.2 (mailto:{_contact}; https://github.com/citation-analyzer)"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _build_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Return base headers merged with any caller-supplied extras."""
    h = {"User-Agent": POLITE_USER_AGENT}
    if extra:
        h.update(extra)
    return h


def _get_with_retry(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    *,
    source_label: str = "API",
    max_retries: Optional[int] = None,
    timeout: float = 20.0,
) -> Optional[Dict]:
    """
    GET ``url`` with:
      - Polite pacing (REQUEST_DELAY between calls)
      - Exponential backoff on HTTP 429 (Too Many Requests) and 503
      - Retry up to max_retries times on transient network/HTTP errors
      - Returns parsed JSON dict on success, or None on permanent failure.

    The ``source_label`` is used only for readable log messages.
    """
    merged_headers = _build_headers(headers)
    retries_limit = max_retries if max_retries is not None else MAX_RETRIES

    for attempt in range(retries_limit + 1):
        try:
            request_params = {
                key: value
                for key, value in (params or {}).items()
                if value not in (None, "")
            }
            if "S2/" in source_label:
                request_gate = _S2_REQUEST_GATE
            elif "Crossref/" in source_label:
                request_gate = _CROSSREF_REQUEST_GATE
            else:
                request_gate = nullcontext()

            with request_gate:
                time.sleep(REQUEST_DELAY)
                resp = requests.get(
                    url,
                    params=request_params,
                    headers=merged_headers,
                    timeout=timeout,
                )

            # Rate-limited or server overload — back off and retry
            if resp.status_code in (429, 503):
                retry_after = int(resp.headers.get("Retry-After", 0))
                wait = min(max(retry_after, BACKOFF_BASE ** attempt), 2.5)
                logger.warning(
                    "[%s] HTTP %d — backing off %.1fs (attempt %d/%d): %s",
                    source_label, resp.status_code, wait, attempt + 1, retries_limit, url,
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.Timeout:
            wait = BACKOFF_BASE ** attempt
            logger.warning(
                "[%s] Timeout (attempt %d/%d) — retry in %.1fs: %s",
                source_label, attempt + 1, retries_limit, wait, url,
            )
            time.sleep(wait)

        except requests.exceptions.ConnectionError as exc:
            wait = BACKOFF_BASE ** attempt
            logger.warning(
                "[%s] Connection error (attempt %d/%d) — retry in %.1fs: %s — %s",
                source_label, attempt + 1, retries_limit, wait, url, exc,
            )
            time.sleep(wait)

        except requests.exceptions.HTTPError as exc:
            # Non-retryable HTTP error (4xx that isn't 429)
            logger.warning("[%s] HTTP error — not retrying: %s — %s", source_label, url, exc)
            return None

        except requests.RequestException as exc:
            logger.warning("[%s] Request failed: %s — %s", source_label, url, exc)
            return None

    logger.error("[%s] All %d retries exhausted for: %s", source_label, retries_limit, url)
    return None


# Keep the old _get alias for backward-compat with Layer 1 callers
def _get(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Optional[Dict]:
    return _get_with_retry(url, params, headers, source_label="generic")


# ---------------------------------------------------------------------------
# Normalisation helpers shared by Layer 1 & 2
# ---------------------------------------------------------------------------

_ORCID_RE = re.compile(
    r"(?:https?://orcid\.org/)?(\d{4}-\d{4}-\d{4}-\d{3}[\dX])",
    re.IGNORECASE,
)
_DOI_RE = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)",
    re.IGNORECASE,
)


def _strip_openalex_prefix(url_or_id: str) -> str:
    return url_or_id.replace("https://openalex.org/", "").strip()


def _clean_doi(raw: Optional[str]) -> Optional[str]:
    """Strip the https://doi.org/ prefix and lowercase."""
    if not raw:
        return None
    return raw.replace("https://doi.org/", "").strip().lower() or None


def _extract_affiliation(author_data: Dict) -> Optional[str]:
    for field in ("affiliations", "last_known_institutions"):
        entries = author_data.get(field) or []
        if entries:
            inst = entries[0].get("institution") or entries[0]
            name = inst.get("display_name")
            if name:
                return name
    return None


def _author_to_candidate(author: Dict) -> Dict:
    return {
        "openalex_id": _strip_openalex_prefix(author.get("id", "")),
        "display_name": author.get("display_name", ""),
        "orcid": author.get("orcid"),
        "affiliation": _extract_affiliation(author),
        "works_count": author.get("works_count", 0),
    }


# ---------------------------------------------------------------------------
# Layer 1 — public API
# ---------------------------------------------------------------------------


# Regex for bare OpenAlex author IDs, e.g. "A5039581335"
_OPENALEX_AUTHOR_ID_RE = re.compile(r"^A\d+$", re.IGNORECASE)


def resolve_researcher(query: str) -> List[Dict]:
    """
    Resolve a researcher query (name, ORCID, DOI, or OpenAlex ID) and return
    up to three candidate author profiles for human disambiguation.

    Resolution logic
    ----------------
    1. **OpenAlex author ID** (e.g. ``A5039581335``) → direct author fetch
    2. **ORCID detected** → ``GET /authors/orcid:{orcid}``
    3. **DOI detected**   → ``GET /works/doi:{doi}`` → extract first 3 authorships
    4. **Name**           → ``GET /authors?search={query}&per-page=3``

    Returns
    -------
    list[dict]  each with: openalex_id, display_name, orcid, affiliation, works_count
    """
    query = query.strip()

    # OpenAlex author ID (bare, e.g. "A5039581335" or with prefix "https://openalex.org/A...")
    bare_id = _strip_openalex_prefix(query)  # strips URL prefix if present
    if _OPENALEX_AUTHOR_ID_RE.fullmatch(bare_id):
        logger.info("resolve_researcher: OpenAlex author-ID path → %s", bare_id)
        data = _get_with_retry(
            f"{OPENALEX_BASE}/authors/{bare_id}",
            params={"mailto": OPENALEX_EMAIL},
            source_label="OpenAlex/author-id",
        )
        if data and data.get("id"):
            return [_author_to_candidate(data)]
        logger.warning("OpenAlex author-ID lookup returned no results for %s", bare_id)
        return []

    # ORCID
    orcid_match = _ORCID_RE.fullmatch(query) or _ORCID_RE.search(query)
    if orcid_match:
        bare_orcid = orcid_match.group(1)
        logger.info("resolve_researcher: ORCID path → %s", bare_orcid)
        data = _get_with_retry(
            f"{OPENALEX_BASE}/authors",
            params={
                "filter": f"orcid:https://orcid.org/{bare_orcid}",
                "sort": "works_count:desc",
                "per-page": 3,
                "mailto": OPENALEX_EMAIL,
            },
            source_label="OpenAlex/authors-orcid",
        )
        if data and data.get("results"):
            return [_author_to_candidate(a) for a in data["results"][:3]]

        # Fallback to direct /authors/orcid:{orcid}
        single = _get_with_retry(
            f"{OPENALEX_BASE}/authors/orcid:{bare_orcid}",
            params={"mailto": OPENALEX_EMAIL},
            source_label="OpenAlex/author-orcid",
        )
        if single and single.get("id"):
            return [_author_to_candidate(single)]
        logger.warning("ORCID lookup returned no results for %s", bare_orcid)
        return []

    # DOI
    doi_match = _DOI_RE.fullmatch(query) or _DOI_RE.search(query)
    if doi_match:
        bare_doi = doi_match.group(1).lower()
        logger.info("resolve_researcher: DOI path → %s", bare_doi)
        data = _get_with_retry(
            f"{OPENALEX_BASE}/works/doi:{bare_doi}",
            params={"mailto": OPENALEX_EMAIL, "select": "id,title,authorships"},
            source_label="OpenAlex/works-doi",
        )
        if not data:
            return []
        candidates: List[Dict] = []
        for authorship in (data.get("authorships") or [])[:3]:
            author = authorship.get("author") or {}
            if not author.get("id"):
                continue
            author_id_bare = _strip_openalex_prefix(author["id"])
            full = _get_with_retry(
                f"{OPENALEX_BASE}/authors/{author_id_bare}",
                params={"mailto": OPENALEX_EMAIL},
                source_label="OpenAlex/author",
            ) or author
            candidates.append(_author_to_candidate(full))
        return candidates

    # Name search
    logger.info("resolve_researcher: name search → '%s'", query)
    data = _get_with_retry(
        f"{OPENALEX_BASE}/authors",
        params={"search": query, "per-page": 3, "mailto": OPENALEX_EMAIL},
        source_label="OpenAlex/authors-search",
    )
    if not data or not data.get("results"):
        return []
    return [_author_to_candidate(a) for a in data["results"][:3]]


def get_researcher_publications(openalex_author_id: str) -> List[Dict]:
    """
    Fetch all works by an OpenAlex author ID (cursor-paginated, 200/page).

    Returns list[dict] with: work_id, title, year, doi
    """
    author_id = _strip_openalex_prefix(openalex_author_id)
    publications: List[Dict] = []
    cursor = "*"
    page = 1
    page_size = 200

    while True:
        data = _get_with_retry(
            f"{OPENALEX_BASE}/works",
            params={
                "filter": f"author.id:{author_id}",
                "per-page": page_size,
                **({"cursor": cursor} if OPENALEX_EMAIL else {"page": page}),
                "mailto": OPENALEX_EMAIL,
                "select": "id,title,doi,publication_year,authorships",
            },
            source_label="OpenAlex/works-by-author",
        )
        if not data:
            break

        results = data.get("results", [])
        if not results:
            break

        for work in results:
            authorships = work.get("authorships") or []
            publications.append({
                "work_id": _strip_openalex_prefix(work.get("id", "")),
                "title": work.get("title") or "",
                "year": work.get("publication_year"),
                "doi": _clean_doi(work.get("doi")),
                "authors": [
                    (entry.get("author") or {}).get("display_name", "")
                    for entry in authorships
                ],
                "author_ids": [
                    {"openalex_id": _strip_openalex_prefix((entry.get("author") or {}).get("id") or "")}
                    for entry in authorships
                ],
            })

        if OPENALEX_EMAIL:
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
        else:
            page += 1
            if len(results) < page_size:
                break

    logger.info("get_researcher_publications: %d works for author %s", len(publications), author_id)
    return publications


# ---------------------------------------------------------------------------
# Layer 2 — per-source adapters (internal)
# ---------------------------------------------------------------------------


def _citing_papers_openalex(
    cited_work_id: str,
    cited_doi: Optional[str],
    max_results: Optional[int] = None,
) -> List[Dict]:
    """
    Fetch papers that cite ``cited_work_id`` from OpenAlex.

    Uses filter=cites:{work_id} with cursor pagination (200 results/page).
    This is the most complete and reliable source for citation retrieval.

    Parameters
    ----------
    max_results:
        Optional cap on total records returned.  Useful for tests/demos where
        a highly-cited paper would otherwise paginate for minutes.
    """
    work_id = _strip_openalex_prefix(cited_work_id)
    records: List[Dict] = []
    cursor = "*"
    page_size = min(200, max_results) if max_results else 200
    page = 1

    while True:
        if max_results and len(records) >= max_results:
            break
        data = _get_with_retry(
            f"{OPENALEX_BASE}/works",
            params={
                "filter": f"cites:{work_id}",
                "per-page": page_size,
                **({"cursor": cursor} if OPENALEX_EMAIL else {"page": page}),
                "mailto": OPENALEX_EMAIL,
                "select": "id,title,doi,publication_year,primary_location,best_oa_location,authorships",
            },
            source_label="OpenAlex/cites",
        )
        if not data:
            break

        results = data.get("results", [])
        if not results:
            break

        for work in results:
            loc = work.get("primary_location") or {}
            best = work.get("best_oa_location") or loc
            src = loc.get("source") or {}
            authorships = work.get("authorships", [])
            authors = [(entry.get("author") or {}).get("display_name", "") for entry in authorships]
            author_ids = [
                {"openalex_id": _strip_openalex_prefix((entry.get("author") or {}).get("id") or "")}
                for entry in authorships
            ]
            author_orcids = [
                (entry.get("author") or {}).get("orcid")
                for entry in authorships
            ]
            author_affiliations = [
                ", ".join(
                    institution.get("display_name", "")
                    for institution in entry.get("institutions", [])
                    if institution.get("display_name")
                )
                for entry in authorships
            ]
            records.append({
                "title": work.get("title") or "",
                "authors": authors,
                "author_ids": author_ids,
                "author_orcids": author_orcids,
                "author_affiliations": author_affiliations,
                "year": work.get("publication_year"),
                "doi": _clean_doi(work.get("doi")),
                "venue": src.get("display_name"),
                "publisher_url": (best or {}).get("landing_page_url"),
                "source": "openalex",
                "source_id": _strip_openalex_prefix(work.get("id", "")),
                "cited_work_id": cited_work_id,
                "cited_doi": cited_doi,
            })

        if OPENALEX_EMAIL:
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
        else:
            page += 1
            if len(results) < page_size:
                break

    logger.info("[OpenAlex] %d citing papers for work %s", len(records), work_id)
    return records



def _citing_papers_semantic_scholar(
    cited_doi: str,
    max_results: Optional[int] = None,
) -> List[Dict]:
    """
    Fetch citing papers from the Semantic Scholar Graph API.

    Step 1: Resolve DOI → S2 paperId via GET /paper/DOI:{doi}
    Step 2: Paginate GET /paper/{id}/citations (offset-based, max 500/page)

    Parameters
    ----------
    max_results:
        Optional cap on total records returned.
    """
    extra_headers: Dict[str, str] = {}
    if SEMANTIC_SCHOLAR_API_KEY:
        extra_headers["x-api-key"] = SEMANTIC_SCHOLAR_API_KEY

    # --- Step 1: Resolve DOI → S2 paper ID ---
    paper_data = _get_with_retry(
        f"{S2_BASE}/paper/DOI:{cited_doi}",
        params={"fields": "paperId,title"},
        headers=extra_headers,
        source_label="S2/resolve-doi",
        max_retries=1,
        timeout=8.0,
    )
    if not paper_data or "paperId" not in paper_data:
        logger.info("[S2] Could not resolve DOI to S2 paper ID: %s", cited_doi)
        return []

    paper_id = paper_data["paperId"]
    records: List[Dict] = []
    offset = 0
    limit = min(500, max_results) if max_results else 500  # S2 max per page

    # --- Step 2: Paginate citations ---
    while True:
        if max_results and len(records) >= max_results:
            break
        data = _get_with_retry(
            f"{S2_BASE}/paper/{paper_id}/citations",
            params={
                "fields": "title,authors,externalIds,year,venue,url,publicationVenue",
                "limit": limit,
                "offset": offset,
            },
            headers=extra_headers,
            source_label="S2/citations",
            max_retries=1,
            timeout=8.0,
        )
        if not data:
            break

        citing_papers = data.get("data") or []
        if not citing_papers:
            break

        for item in citing_papers:
            citing = item.get("citingPaper") or {}
            ext = citing.get("externalIds") or {}
            authors = [a.get("name", "") for a in (citing.get("authors") or [])]
            pub_venue = citing.get("publicationVenue") or {}

            records.append({
                "title": citing.get("title") or "",
                "authors": authors,
                "year": citing.get("year"),
                "doi": (ext.get("DOI") or "").lower() or None,
                "venue": pub_venue.get("name") or citing.get("venue"),
                "publisher_url": citing.get("url"),
                "source": "semantic_scholar",
                "source_id": citing.get("paperId"),
                "cited_work_id": None,
                "cited_doi": cited_doi,
            })

        if len(citing_papers) < limit:
            break
        offset += limit

    logger.info("[S2] %d citing papers for DOI %s (paper_id=%s)", len(records), cited_doi, paper_id)
    return records


def get_crossref_metadata(doi: str) -> Dict[str, Any]:
    """
    Lookup metadata for a single work by DOI from Crossref works API
    (GET https://api.crossref.org/works/{doi}).

    Used ONLY for metadata enrichment (filling in venue, publisher, full author list
    on records obtained from citation indexes) — NEVER to discover new citing candidates.
    """
    if not doi:
        return {}

    clean_doi = doi.lower().strip()
    # Strip URL prefix if provided
    clean_doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", clean_doi)

    data = _get_with_retry(
        f"{CROSSREF_BASE}/works/{clean_doi}",
        source_label="Crossref/works",
    )
    if not data or not isinstance(data, dict):
        return {}

    item = data.get("message") or {}
    if not item or not isinstance(item, dict):
        return {}

    authors = [
        f"{a.get('given', '')} {a.get('family', '')}".strip()
        for a in (item.get("author") or [])
        if isinstance(a, dict)
    ]
    authors = [a for a in authors if a]

    year = None
    issued = (item.get("issued") or {}).get("date-parts") or [[None]]
    if issued and issued[0] and issued[0][0]:
        try:
            year = int(issued[0][0])
        except (ValueError, TypeError):
            year = None

    container = item.get("container-title") or []
    venue = container[0] if isinstance(container, list) and container else str(container or "") or None

    title_raw = item.get("title") or []
    title = title_raw[0] if isinstance(title_raw, list) and title_raw else str(title_raw or "") or None

    publisher = item.get("publisher") or None

    return {
        "title": title,
        "authors": authors,
        "year": year,
        "venue": venue,
        "publisher": publisher,
        "doi": clean_doi,
    }


def get_opencitations_citations(
    cited_doi: str,
    max_results: Optional[int] = None,
    enrich_metadata: bool = True,
) -> List[Dict]:
    """
    Fetch real citing papers from OpenCitations (COCI index).
    API: GET https://opencitations.net/index/coci/api/v1/citations/{doi}

    Returns real citing DOIs. For each citing DOI, resolves basic metadata
    via Crossref works API (get_crossref_metadata).
    """
    if not cited_doi:
        return []

    clean_doi = cited_doi.lower().strip()
    clean_doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", clean_doi)

    data = _get_with_retry(
        f"{OPENCITATIONS_BASE}/citations/{clean_doi}",
        source_label="OpenCitations/COCI",
        max_retries=1,
        timeout=8.0,
    )

    if not data or not isinstance(data, list):
        return []

    records: List[Dict] = []
    limit = min(len(data), max_results) if max_results else len(data)

    def build_record(item: Dict) -> Optional[Dict]:
        if not isinstance(item, dict):
            return None
        citing_doi = (item.get("citing") or "").lower().strip() or None
        if not citing_doi:
            return None
        # Skip self-citation loop if same DOI
        if citing_doi == clean_doi:
            return None

        # DOI is sufficient for deduplication; Crossref enrichment is
        # intentionally optional because large citation graphs can contain
        # thousands of DOI records.
        meta = get_crossref_metadata(citing_doi) if enrich_metadata else {}

        return {
            "title": meta.get("title") or f"Citing Paper ({citing_doi})",
            "authors": meta.get("authors") or [],
            "year": meta.get("year"),
            "doi": citing_doi,
            "venue": meta.get("venue"),
            "publisher": meta.get("publisher"),
            "source": "opencitations",
            "source_id": citing_doi,
            "cited_work_id": None,
            "cited_doi": clean_doi,
        }

    with ThreadPoolExecutor(max_workers=RETRIEVAL_WORKERS) as executor:
        for record in executor.map(build_record, data[:limit]):
            if record:
                records.append(record)

    logger.info("[OpenCitations] %d real citing records for DOI %s", len(records), clean_doi)
    return records



def get_ieee_scopus_wos_citations(
    cited_doi: Optional[str] = None,
    cited_work_id: Optional[str] = None,
) -> List[Dict]:
    """
    Stub for IEEE Xplore, Scopus, and Web of Science citation retrieval.

    TODO: Implement when institutional API keys are available.
          - IEEE Xplore: https://developer.ieee.org/apis
          - Scopus: https://dev.elsevier.com/sc_apis.html (ELS-APIKey required)
          - Web of Science: https://developer.clarivate.com (WoS API key required)

    These three sources are documented in the project synopsis as future-work
    sources that require paid/institutional access — they do not block the
    core pipeline which uses OpenAlex + Crossref + Semantic Scholar.
    """
    logger.debug(
        "get_ieee_scopus_wos_citations: stub called for doi=%s work_id=%s — "
        "skipping (institutional API keys not configured)",
        cited_doi, cited_work_id,
    )
    return []


# ---------------------------------------------------------------------------
# Layer 2 — public API
# ---------------------------------------------------------------------------


def get_citing_papers(
    cited_work_id: str,
    cited_doi: Optional[str] = None,
    cited_title: Optional[str] = None,
    max_results_per_source: Optional[int] = None,
    enrich_crossref: Optional[bool] = None,
) -> List[Dict]:
    """
    Retrieve all raw citing papers for a single work from all available sources.

    Each source is called independently; if one fails it is logged and the
    others continue — no single source failure crashes the pipeline.

    Parameters
    ----------
    cited_work_id:
        OpenAlex work ID (bare, e.g. ``"W2741809807"``).
    cited_doi:
        Optional bare DOI (used for Semantic Scholar & OpenCitations lookup).
    cited_title:
        Optional paper title.

    Returns
    -------
    list[dict]  — flat, unnormalised raw records from all sources.
    Each record has:
        title, authors (list[str]), year, doi, venue,
        source ("openalex" | "semantic_scholar" | "opencitations"),
        source_id, cited_work_id, cited_doi
    """
    if enrich_crossref is None:
        enrich_crossref = max_results_per_source is not None or ENRICH_CROSSREF_UNCAPPED

    source_tasks = [
        ("OpenAlex", lambda: _citing_papers_openalex(
            cited_work_id, cited_doi, max_results=max_results_per_source
        )),
    ]
    include_s2 = bool(
        cited_doi
        and (SEMANTIC_SCHOLAR_API_KEY or ENABLE_S2_UNCAPPED)
    )
    if include_s2:
        source_tasks.extend([
            ("Semantic Scholar", lambda: _citing_papers_semantic_scholar(
                cited_doi, max_results=max_results_per_source
            )),
            ("OpenCitations", lambda: get_opencitations_citations(
                cited_doi,
                max_results=max_results_per_source,
                enrich_metadata=enrich_crossref,
            )),
        ])
    elif cited_doi:
        source_tasks.append(("OpenCitations", lambda: get_opencitations_citations(
            cited_doi,
            max_results=max_results_per_source,
            enrich_metadata=enrich_crossref,
        )))
    source_tasks.append(("IEEE/Scopus/WoS", lambda: get_ieee_scopus_wos_citations(
        cited_doi=cited_doi, cited_work_id=cited_work_id
    )))

    all_records: List[Dict] = []
    with ThreadPoolExecutor(max_workers=len(source_tasks)) as executor:
        futures = [(label, executor.submit(task)) for label, task in source_tasks]
        for label, future in futures:
            try:
                all_records.extend(future.result())
            except Exception as exc:
                logger.error("[%s] Unexpected error for work %s: %s", label, cited_work_id, exc)

    logger.info(
        "get_citing_papers: work=%s doi=%s → %d total raw records",
        cited_work_id, cited_doi, len(all_records),
    )
    return all_records


def get_all_citations_for_researcher(
    openalex_author_id: str,
    *,
    researcher_name: Optional[str] = None,
    max_publications: Optional[int] = None,
    max_results_per_source: Optional[int] = None,
) -> Dict[str, Any]:
    """
    End-to-end Layer 2 entry point:

    1. Fetch all publications for the researcher (Layer 1 → Layer 2 bridge)
    2. For each publication, retrieve all citing papers from all sources
    3. Return the full flat raw citation list plus per-source counts

    Parameters
    ----------
    openalex_author_id:
        Bare OpenAlex author ID (e.g. ``"A5086198262"``).
    researcher_name:
        Optional display name.
    max_publications:
        Optional cap on how many publications to process. Useful for testing
        without exhausting API rate limits (e.g. set to 5 for a quick smoke test).

    Returns
    -------
    dict with keys:
        publications   : list[dict]  — all fetched publications
        raw_citations  : list[dict]  — all raw citing records (unnormalised)
        counts_by_source : dict      — {source_name: count}
        total          : int         — total raw citation records
    """
    publications = get_researcher_publications(openalex_author_id)
    if max_publications is not None:
        publications = publications[:max_publications]

    logger.info(
        "get_all_citations_for_researcher: processing %d publications for author %s (name=%s)",
        len(publications), openalex_author_id, researcher_name,
    )

    def fetch_publication_citations(pub: Dict) -> List[Dict]:
        return get_citing_papers(
            cited_work_id=pub["work_id"],
            cited_doi=pub.get("doi"),
            cited_title=pub.get("title"),
            max_results_per_source=max_results_per_source,
        )

    all_citations: List[Dict] = []
    with ThreadPoolExecutor(max_workers=RETRIEVAL_WORKERS) as executor:
        for citing in executor.map(fetch_publication_citations, publications):
            all_citations.extend(citing)

    counts = Counter(r.get("source", "unknown") for r in all_citations)

    logger.info(
        "get_all_citations_for_researcher: done — %d total raw records | %s",
        len(all_citations),
        dict(counts),
    )

    return {
        "publications": publications,
        "raw_citations": all_citations,
        "counts_by_source": dict(counts),
        "total": len(all_citations),
    }
