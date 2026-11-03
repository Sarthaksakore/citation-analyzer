"""Optional Google Scholar profile lookup through SerpApi."""

from __future__ import annotations

import os
import re
import logging
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv

load_dotenv()

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
GOOGLE_SCHOLAR_AUTHOR_ID = os.getenv("GOOGLE_SCHOLAR_AUTHOR_ID", "")
GOOGLE_SCHOLAR_AUTHOR_NAME = os.getenv("GOOGLE_SCHOLAR_AUTHOR_NAME", "")
SERPAPI_URL = "https://serpapi.com/search.json"
SCHOLAR_CITATION_WORKERS = max(1, int(os.getenv("SCHOLAR_CITATION_WORKERS", "4")))
logger = logging.getLogger(__name__)


def _name_score(query: str, candidate: str) -> float:
    normalized_query = re.sub(r"[^a-z0-9 ]", "", query.lower()).strip()
    normalized_candidate = re.sub(r"[^a-z0-9 ]", "", candidate.lower()).strip()
    return SequenceMatcher(None, normalized_query, normalized_candidate).ratio()


def _request(params: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.get(
        SERPAPI_URL,
        params={**params, "api_key": SERPAPI_KEY},
        timeout=15,
    )
    if not response.ok:
        try:
            detail = response.json().get("error") or response.reason
        except ValueError:
            detail = response.reason
        raise RuntimeError(f"HTTP {response.status_code}: {detail}")
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(payload["error"])
    return payload


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
        "reason": "Profile lookup is unavailable; showing name-based Scholar results",
    }


def _get_author_profile(researcher_name: str, author_id: str) -> Dict[str, Any]:
    details = _request({"engine": "google_scholar_author", "author_id": author_id})
    first_details = details
    all_articles = list(details.get("articles") or [])
    start = len(all_articles)
    while details.get("serpapi_pagination", {}).get("next"):
        details = _request({
            "engine": "google_scholar_author",
            "author_id": author_id,
            "start": start,
        })
        page_articles = details.get("articles") or []
        if not page_articles:
            break
        all_articles.extend(page_articles)
        start += len(page_articles)
        if len(page_articles) < 20:
            break

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

    def fetch_citing_papers(article: Dict[str, Any]) -> List[Dict[str, Any]]:
        cites_id = article.get("cites_id")
        if not cites_id:
            return []
        try:
            results = _request({
                "engine": "google_scholar",
                "cites": cites_id,
                "num": 100,
            }).get("organic_results") or []
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            logger.warning("Google Scholar citation cluster %s unavailable: %s", cites_id, exc)
            return []
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
        return citing_papers

    citing_papers: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=SCHOLAR_CITATION_WORKERS) as executor:
        for papers in executor.map(fetch_citing_papers, articles):
            citing_papers.extend(papers)

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
        "citing_papers": citing_papers,
        "citing_paper_count": len(citing_papers),
        "raw_citing_paper_count": raw_citing_paper_count,
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
