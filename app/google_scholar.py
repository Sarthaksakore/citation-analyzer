"""Optional Google Scholar profile lookup through SerpApi."""

from __future__ import annotations

import os
import re
import logging
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
GOOGLE_SCHOLAR_AUTHOR_ID = os.getenv("GOOGLE_SCHOLAR_AUTHOR_ID", "")
GOOGLE_SCHOLAR_AUTHOR_NAME = os.getenv("GOOGLE_SCHOLAR_AUTHOR_NAME", "")
SERPAPI_URL = "https://serpapi.com/search.json"
SCHOLAR_CITATION_WORKERS = max(1, int(os.getenv("SCHOLAR_CITATION_WORKERS", "2")))
SCHOLAR_MAX_PAGES = max(1, int(os.getenv("SCHOLAR_MAX_PAGES", "1000")))
SCHOLAR_CACHE_TTL_SECONDS = max(0.0, float(os.getenv("SCHOLAR_CACHE_TTL_SECONDS", "900")))
_request_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
_request_cache_lock = threading.Lock()
_serpapi_quota_exhausted = threading.Event()
logger = logging.getLogger(__name__)


def _name_score(query: str, candidate: str) -> float:
    normalized_query = re.sub(r"[^a-z0-9 ]", "", query.lower()).strip()
    normalized_candidate = re.sub(r"[^a-z0-9 ]", "", candidate.lower()).strip()
    return SequenceMatcher(None, normalized_query, normalized_candidate).ratio()


def _request(params: Dict[str, Any]) -> Dict[str, Any]:
    if _serpapi_quota_exhausted.is_set():
        raise RuntimeError("SerpApi search quota is exhausted")
    cache_key = json.dumps(params, sort_keys=True, default=str)
    if SCHOLAR_CACHE_TTL_SECONDS:
        with _request_cache_lock:
            cached = _request_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < SCHOLAR_CACHE_TTL_SECONDS:
                return json.loads(json.dumps(cached[1]))
            if cached:
                _request_cache.pop(cache_key, None)
    last_error = None
    for attempt in range(4):
        try:
            response = requests.get(
                SERPAPI_URL,
                params={**params, "api_key": SERPAPI_KEY},
                timeout=15,
            )
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(min(2 ** attempt, 15))
            continue
        if response.status_code != 429:
            break
        try:
            error_detail = str(response.json().get("error") or "").lower()
        except ValueError:
            error_detail = response.text.lower()
        if "run out of searches" in error_detail or "searches left" in error_detail:
            _serpapi_quota_exhausted.set()
            raise RuntimeError("SerpApi search quota is exhausted")
        retry_after = response.headers.get("Retry-After")
        try:
            wait = float(retry_after) if retry_after else min(2 ** (attempt + 1), 30)
        except ValueError:
            wait = min(2 ** (attempt + 1), 30)
        time.sleep(wait)
    else:
        raise RuntimeError(f"SerpApi request failed after retries: {last_error or response.reason}")
    if not response.ok:
        try:
            detail = response.json().get("error") or response.reason
        except ValueError:
            detail = response.reason
        raise RuntimeError(f"HTTP {response.status_code}: {detail}")
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(payload["error"])
    if SCHOLAR_CACHE_TTL_SECONDS:
        with _request_cache_lock:
            _request_cache[cache_key] = (time.monotonic(), payload)
    return payload


def _result_key(result: Dict[str, Any]) -> str:
    """Build a stable key for pagination de-duplication."""
    return str(result.get("result_id") or (
        (result.get("title") or "").strip().lower(),
        result.get("year"),
    ))


def _fetch_all_results(
    params: Dict[str, Any],
    result_key: str,
    page_size: int,
    first_page: Optional[Dict[str, Any]] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch every page exposed by SerpApi's pagination metadata."""
    results: List[Dict[str, Any]] = []
    seen_keys = set()
    page = first_page
    start = 0
    pages_fetched = 0
    complete = True
    stop_reason = "no_next_page"
    for page_number in range(SCHOLAR_MAX_PAGES):
        if page is None:
            page_params = {**params, "start": start}
            if page_size:
                page_params["num"] = page_size
            page = _request(page_params)
        page_results = page.get(result_key) or []
        pages_fetched += 1
        if not page_results:
            stop_reason = "empty_page"
            break
        new_results = 0
        for result in page_results:
            key = _result_key(result)
            if key not in seen_keys:
                seen_keys.add(key)
                results.append(result)
                new_results += 1
        pagination = page.get("serpapi_pagination") or {}
        next_page = pagination.get("next")
        if not next_page or not new_results:
            stop_reason = "duplicate_page" if not new_results else "no_next_page"
            break
        start += len(page_results)
        page = None
    else:
        logger.warning("Google Scholar pagination stopped at %d pages", SCHOLAR_MAX_PAGES)
        complete = False
        stop_reason = "max_pages"
    return results, {
        "pages_fetched": pages_fetched,
        "results_fetched": len(results),
        "complete": complete,
        "stop_reason": stop_reason,
    }


def _search_google_scholar_articles(researcher_name: str) -> Dict[str, Any]:
    search = _request({
        "engine": "google_scholar",
        "q": researcher_name,
        "num": 20,
    })
    articles = []
    profile_candidates: Dict[str, int] = {}
    name_tokens = [
        token.lower()
        for token in re.findall(r"[a-zA-Z]+", researcher_name)
        if len(token) >= 4
    ]
    for result in search.get("organic_results") or []:
        inline_links = result.get("inline_links") or {}
        cited_by = inline_links.get("cited_by") or {}
        publication_info = result.get("publication_info") or {}
        author_entries = publication_info.get("authors") or []
        authors = [author.get("name") for author in author_entries]
        for author in author_entries:
            author_id = author.get("author_id")
            author_name = (author.get("name") or "").lower()
            if author_id and any(token in author_name for token in name_tokens):
                profile_candidates[author_id] = profile_candidates.get(author_id, 0) + 1
        article = {
            "title": result.get("title"),
            "authors": [author for author in authors if author],
            "year": publication_info.get("year"),
            "result_id": result.get("result_id"),
            "cited_by": cited_by.get("value", 0),
            "cited_by_url": cited_by.get("link"),
            "url": result.get("link"),
        }
        if not name_tokens or any(
            token in author.lower()
            for author in article["authors"]
            for token in name_tokens
        ):
            articles.append(article)

    return {
        "enabled": True,
        "source": "google_scholar",
        "matched": bool(articles),
        "match_type": "article_search",
        "query": researcher_name,
        "profile_name": researcher_name,
        "profile_id": max(profile_candidates, key=profile_candidates.get) if profile_candidates else None,
        "citation_metrics": {
            "search_results": sum(article["cited_by"] for article in articles),
        },
        "articles": articles,
        "profile_fetch": {
            "pages_fetched": 1,
            "results_fetched": len(search.get("organic_results") or []),
            "complete": False,
            "stop_reason": "profile_not_resolved",
        },
        "reason": "Profile lookup is unavailable; showing name-based Scholar results",
    }


def _get_author_profile(researcher_name: str, author_id: str) -> Dict[str, Any]:
    details = _request({"engine": "google_scholar_author", "author_id": author_id})
    first_details = details
    all_articles, article_pagination = _fetch_all_results(
        {"engine": "google_scholar_author", "author_id": author_id},
        "articles",
        page_size=20,
        first_page=details,
    )
    profile_details = first_details
    author = profile_details.get("author") or {}
    raw_metrics = profile_details.get("cited_by") or {}
    metrics: Dict[str, Any] = {}
    metric_names = {
        "citations": "citations",
        "h_index": "h_index",
        "i10_index": "i10_index",
    }
    for row in raw_metrics.get("table") or []:
        for metric_name, values in row.items():
            output_name = metric_names.get(metric_name, metric_name)
            metrics[output_name] = values.get("all")
            metrics[f"{output_name}_since_2021"] = values.get("since_2021")
    metrics["all"] = metrics.get("citations")
    metrics["since_2021"] = metrics.get("citations_since_2021")
    metrics["graph"] = raw_metrics.get("graph") or []
    articles = []
    for article in all_articles:
        cited_by = article.get("cited_by") or {}
        articles.append({
            "title": article.get("title"),
            "year": article.get("year"),
            "citation_id": article.get("citation_id"),
            "cited_by": cited_by.get("value", 0),
            "cites_id": cited_by.get("cites_id"),
            "cited_by_url": cited_by.get("link"),
            "url": article.get("link"),
        })

    def fetch_citing_papers(article: Dict[str, Any]) -> Dict[str, Any]:
        cites_id = article.get("cites_id")
        if not cites_id:
            return {"papers": [], "pages": 0, "complete": True, "error": None}
        try:
            results, pagination = _fetch_all_results({
                "engine": "google_scholar",
                "cites": cites_id,
            }, "organic_results", page_size=100)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            logger.warning("Google Scholar citation cluster %s unavailable: %s", cites_id, exc)
            return {"papers": [], "pages": 0, "complete": False, "error": str(exc)}
        citing_papers = []
        for result in results:
            publication_info = result.get("publication_info") or {}
            citing_papers.append({
                "title": result.get("title"),
                "authors": [
                    author.get("name")
                    for author in publication_info.get("authors") or []
                    if author.get("name")
                ],
                "year": publication_info.get("year"),
                "venue": publication_info.get("summary"),
                "source": "google_scholar",
                "source_id": result.get("result_id"),
                "url": result.get("link"),
                "cited_article_title": article.get("title"),
                "cited_article_id": article.get("citation_id"),
            })
        return {
            "papers": citing_papers,
            "pages": pagination["pages_fetched"],
            "complete": pagination["complete"],
            "error": None,
        }

    articles_by_cites_id: Dict[str, Dict[str, Any]] = {}
    articles_without_cites_id: List[Dict[str, Any]] = []
    for article in articles:
        cites_id = article.get("cites_id")
        if cites_id:
            articles_by_cites_id.setdefault(cites_id, article)
        else:
            articles_without_cites_id.append(article)

    citing_papers: List[Dict[str, Any]] = []
    cluster_results = []
    with ThreadPoolExecutor(max_workers=SCHOLAR_CITATION_WORKERS) as executor:
        cluster_results.extend(executor.map(fetch_citing_papers, articles_by_cites_id.values()))
        cluster_results.extend(executor.map(fetch_citing_papers, articles_without_cites_id))
    for result in cluster_results:
        citing_papers.extend(result["papers"])

    raw_citing_paper_count = len(citing_papers)
    unique_citing_papers: Dict[str, Dict[str, Any]] = {}
    for paper in citing_papers:
        title = (paper.get("title") or "").strip().lower()
        key = (
            "|".join([title, str(paper.get("year") or "")])
            if title
            else paper.get("source_id")
        )
        unique_citing_papers.setdefault(key, paper)
    citing_papers = list(unique_citing_papers.values())

    return {
        "enabled": True,
        "source": "google_scholar",
        "matched": True,
        "match_type": "author_profile",
        "query": researcher_name,
        "profile_id": author_id,
        "profile_name": author.get("name") or researcher_name,
        "profile_url": author.get("link") or f"https://scholar.google.com/citations?user={author_id}",
        "affiliation": author.get("affiliations"),
        "citation_metrics": metrics,
        "articles": articles,
        "profile_article_count": len(articles),
        "profile_citation_total": metrics.get("all"),
        "profile_fetch": article_pagination,
        "citing_papers": citing_papers,
        "citing_paper_count": len(citing_papers),
        "raw_citing_paper_count": raw_citing_paper_count,
        "citing_paper_fetch": {
            "clusters_requested": len(articles),
            "clusters_with_cites_id": len(articles_by_cites_id),
            "clusters_succeeded": sum(result["error"] is None for result in cluster_results),
            "clusters_failed": sum(result["error"] is not None for result in cluster_results),
            "pages_fetched": sum(result["pages"] for result in cluster_results),
            "complete": all(result["complete"] for result in cluster_results),
            "errors": [result["error"] for result in cluster_results if result["error"]],
        },
    }


def get_google_scholar_profile(researcher_name: str) -> Dict[str, Any]:
    """Resolve a researcher name through the supported Scholar search API."""
    if not SERPAPI_KEY:
        return {
            "enabled": False,
            "source": "google_scholar",
            "reason": "SERPAPI_KEY is not configured",
        }

    try:
        if (
            GOOGLE_SCHOLAR_AUTHOR_ID
            and GOOGLE_SCHOLAR_AUTHOR_NAME
            and _name_score(researcher_name, GOOGLE_SCHOLAR_AUTHOR_NAME) >= 55.0
        ):
            return _get_author_profile(researcher_name, GOOGLE_SCHOLAR_AUTHOR_ID)
        search_result = _search_google_scholar_articles(researcher_name)
        profile_id = search_result.get("profile_id")
        if profile_id:
            return _get_author_profile(researcher_name, profile_id)
        return search_result
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        return {
            "enabled": True,
            "source": "google_scholar",
            "matched": False,
            "query": researcher_name,
            "reason": f"Google Scholar provider error: {exc}",
        }
