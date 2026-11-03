"""
app/storage.py — Layer 5: SQLAlchemy ORM models, session helpers, and
                 pipeline persistence.

Tables
------
  researchers     — resolved researcher profiles
  publications    — publications belonging to a researcher
  citations       — deduplicated, classified citing papers (Layer 3+4 output)

Persistence API
---------------
  save_pipeline_run(researcher_dict, publications, classified_citations, db)
      Persist a full pipeline run atomically.

  get_researcher_citations(openalex_id, db)
      Re-query all stored citations for a researcher without calling APIs.

  upsert_researcher(researcher_dict, db)
      Insert-or-update a researcher record.

Usage
-----
  DATABASE_URL in .env controls the backend.
  Default: sqlite:///./citation_analyzer.db
  Swap to PostgreSQL: DATABASE_URL=postgresql://user:pass@host/db
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

load_dotenv()

logger = logging.getLogger(__name__)

DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./citation_analyzer.db")

# SQLite needs check_same_thread=False for multi-threaded FastAPI usage
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    # Allow plain Python list[...] annotations on relationships without
    # wrapping them in Mapped[...] — required for SQLAlchemy 2.x strict mode.
    __allow_unmapped__ = True


class DBUser(Base):
    """Registered application user."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String(200), nullable=False)
    email = Column(String(320), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    sessions: list["DBAuthSession"] = relationship(
        "DBAuthSession", back_populates="user", cascade="all, delete-orphan"
    )


class DBAuthSession(Base):
    """Server-side session record; the browser receives only the raw token cookie."""

    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    user: "DBUser" = relationship("DBUser", back_populates="sessions")


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------


class DBResearcher(Base):
    """
    Resolved researcher identity (Layer 1 output).

    ``openalex_id`` is used as the primary natural key for lookups;
    ``orcid`` is stored when available for cross-referencing.
    """

    __tablename__ = "researchers"

    id = Column(Integer, primary_key=True, index=True)
    openalex_id = Column(String, unique=True, index=True, nullable=True)
    orcid = Column(String, unique=True, index=True, nullable=True)
    name = Column(String, nullable=False)
    affiliation = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    publications: list["DBPublication"] = relationship(
        "DBPublication", back_populates="researcher", cascade="all, delete-orphan"
    )


class DBPublication(Base):
    """
    A publication authored by the target researcher (cited work).
    Acts as the anchor for all inbound citation records.
    """

    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("researcher_id", "work_id", name="uq_pub_researcher_work"),)

    id = Column(Integer, primary_key=True, index=True)
    researcher_id = Column(Integer, ForeignKey("researchers.id"), nullable=False, index=True)
    work_id = Column(String, nullable=False, index=True)   # OpenAlex work ID
    title = Column(Text, nullable=False)
    year = Column(Integer, nullable=True)
    doi = Column(String, index=True, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    researcher: "DBResearcher" = relationship("DBResearcher", back_populates="publications")
    citations: list["DBCitation"] = relationship(
        "DBCitation", back_populates="publication", cascade="all, delete-orphan"
    )


class DBCitation(Base):
    """
    A deduplicated, classified citing paper (Layers 3+4 combined output).

    ``sources`` is stored as a JSON array  e.g. '["openalex","crossref"]'.
    ``authors`` is stored as a JSON array of name strings.
    """

    __tablename__ = "citations"

    id = Column(Integer, primary_key=True, index=True)
    publication_id = Column(Integer, ForeignKey("publications.id"), nullable=False, index=True)

    # Bibliographic fields
    title = Column(Text, nullable=False)
    authors = Column(Text, nullable=True)           # JSON list of strings
    year = Column(Integer, nullable=True)
    doi = Column(String, index=True, nullable=True)
    venue = Column(String, nullable=True)

    # Provenance
    sources = Column(Text, nullable=True)           # JSON list of source names
    duplicate_count = Column(Integer, default=1)
    source_record_ids = Column(Text, nullable=True) # JSON list of raw _raw_id UUIDs

    # Layer 4 — classification
    classification = Column(String, default="UNRESOLVED")  # SELF / EXTERNAL / UNRESOLVED
    confidence = Column(Float, default=0.0)
    evidence = Column(Text, nullable=True)          # free-text evidence string
    needs_review = Column(Integer, default=0)       # 0/1 flag (SQLite has no bool)

    retrieved_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    publication: "DBPublication" = relationship("DBPublication", back_populates="citations")


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------


def init_db() -> None:
    """Create all tables if they do not already exist."""
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# FastAPI session dependency
# ---------------------------------------------------------------------------


def get_db():
    """
    Yield a SQLAlchemy session and ensure it is closed after the request::

        @app.get("/example")
        def example(db: Session = Depends(get_db)):
            ...
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def upsert_researcher(researcher_dict: Dict[str, Any], db: Session) -> DBResearcher:
    """
    Insert or update a researcher record from a resolve_researcher() dict.

    Lookup order: openalex_id → orcid → name (last resort).
    Updates ``name``, ``affiliation``, and ``updated_at`` on each run.

    Returns
    -------
    DBResearcher ORM object (already added to session, not yet committed).
    """
    openalex_id = researcher_dict.get("openalex_id")
    orcid = researcher_dict.get("orcid")
    name = researcher_dict.get("display_name") or researcher_dict.get("name") or "Unknown"

    # Try to find an existing record
    row: Optional[DBResearcher] = None
    if openalex_id:
        row = db.query(DBResearcher).filter_by(openalex_id=openalex_id).first()
    if row is None and orcid:
        row = db.query(DBResearcher).filter_by(orcid=orcid).first()
    if row is None and name:
        row = db.query(DBResearcher).filter_by(name=name).first()

    if row is None:
        row = DBResearcher(
            openalex_id=openalex_id,
            orcid=orcid,
            name=name,
            affiliation=researcher_dict.get("affiliation"),
        )
        db.add(row)
    else:
        row.name = name
        row.affiliation = researcher_dict.get("affiliation") or row.affiliation
        row.openalex_id = openalex_id or row.openalex_id
        row.orcid = orcid or row.orcid
        row.updated_at = datetime.now(timezone.utc)

    return row


def _upsert_publication(
    pub_dict: Dict[str, Any],
    researcher_row: DBResearcher,
    db: Session,
) -> DBPublication:
    """Insert or update a single publication record."""
    work_id = pub_dict.get("work_id", "")
    row = (
        db.query(DBPublication)
        .filter_by(researcher_id=researcher_row.id, work_id=work_id)
        .first()
    )
    if row is None:
        row = DBPublication(
            researcher_id=researcher_row.id,
            work_id=work_id,
            title=pub_dict.get("title") or "",
            year=pub_dict.get("year"),
            doi=pub_dict.get("doi"),
        )
        db.add(row)
    else:
        row.title = pub_dict.get("title") or row.title
        row.year = pub_dict.get("year") or row.year
        row.doi = pub_dict.get("doi") or row.doi
    return row


def _upsert_citation(
    citation_dict: Dict[str, Any],
    pub_row: DBPublication,
    db: Session,
) -> DBCitation:
    """
    Insert or update a single citation record.

    Keyed on (publication_id, doi) when DOI is available; otherwise
    keyed on (publication_id, title) as a fallback.
    """
    doi = citation_dict.get("doi")
    title = citation_dict.get("title") or ""

    row: Optional[DBCitation] = None
    if doi:
        row = db.query(DBCitation).filter_by(publication_id=pub_row.id, doi=doi).first()
    if row is None:
        row = db.query(DBCitation).filter_by(publication_id=pub_row.id, title=title).first()

    authors_json = json.dumps(citation_dict.get("authors") or [])
    sources_json = json.dumps(citation_dict.get("sources") or [])
    src_ids_json = json.dumps(citation_dict.get("source_record_ids") or [])

    if row is None:
        row = DBCitation(
            publication_id=pub_row.id,
            title=title,
            authors=authors_json,
            year=citation_dict.get("year"),
            doi=doi,
            venue=citation_dict.get("venue"),
            sources=sources_json,
            duplicate_count=citation_dict.get("duplicate_count", 1),
            source_record_ids=src_ids_json,
            classification=citation_dict.get("classification", "UNRESOLVED"),
            confidence=citation_dict.get("confidence", 0.0),
            evidence=citation_dict.get("evidence"),
            needs_review=int(bool(citation_dict.get("needs_review", False))),
        )
        db.add(row)
    else:
        # Update classification & provenance on re-runs
        row.authors = authors_json
        row.venue = citation_dict.get("venue") or row.venue
        row.sources = sources_json
        row.duplicate_count = citation_dict.get("duplicate_count", row.duplicate_count)
        row.source_record_ids = src_ids_json
        row.classification = citation_dict.get("classification", row.classification)
        row.confidence = citation_dict.get("confidence", row.confidence)
        row.evidence = citation_dict.get("evidence") or row.evidence
        row.needs_review = int(bool(citation_dict.get("needs_review", False)))
        row.retrieved_at = datetime.now(timezone.utc)

    return row


def save_pipeline_run(
    researcher_dict: Dict[str, Any],
    publications: List[Dict[str, Any]],
    classified_citations_by_pub: Dict[str, List[Dict[str, Any]]],
    db: Session,
) -> DBResearcher:
    """
    Atomically persist a full pipeline run to the database.

    Parameters
    ----------
    researcher_dict:
        Dict from ``resolve_researcher()`` (keys: display_name, openalex_id,
        orcid, affiliation).
    publications:
        List of publication dicts from ``get_researcher_publications()``.
        Each has: work_id, title, year, doi.
    classified_citations_by_pub:
        Mapping of work_id → list of classified citation dicts
        (output of ``classify_all()``).  Each dict must have the keys
        produced by ``deduplicate_citations()`` + ``classify_citation()``.
    db:
        Active SQLAlchemy session.

    Returns
    -------
    DBResearcher — the persisted (or updated) researcher row.

    Example
    -------
    ::
        db = SessionLocal()
        try:
            row = save_pipeline_run(researcher, pubs, citations_by_pub, db)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    """
    # 1. Upsert researcher
    researcher_row = upsert_researcher(researcher_dict, db)
    db.flush()  # ensures researcher_row.id is populated

    saved_pubs = 0
    saved_citations = 0
    active_pub_ids = set()

    for pub_dict in publications:
        work_id = pub_dict.get("work_id", "")
        pub_row = _upsert_publication(pub_dict, researcher_row, db)
        db.flush()  # ensures pub_row.id is populated
        active_pub_ids.add(pub_row.id)

        citations_for_pub = classified_citations_by_pub.get(work_id, [])
        saved_cit_ids = set()
        for cit_dict in citations_for_pub:
            cit_row = _upsert_citation(cit_dict, pub_row, db)
            db.flush()
            cit_dict["id"] = cit_row.id
            saved_cit_ids.add(cit_row.id)
            saved_citations += 1

        # Delete any obsolete citations for this publication that are no longer returned
        db.query(DBCitation).filter(
            DBCitation.publication_id == pub_row.id,
            ~DBCitation.id.in_(saved_cit_ids),
        ).delete(synchronize_session=False)

        saved_pubs += 1

    # Remove any old publications (and their citations) belonging to this researcher not in this run
    if active_pub_ids:
        stale_pubs = (
            db.query(DBPublication)
            .filter(
                DBPublication.researcher_id == researcher_row.id,
                ~DBPublication.id.in_(active_pub_ids),
            )
            .all()
        )
        for sp in stale_pubs:
            db.query(DBCitation).filter_by(publication_id=sp.id).delete(synchronize_session=False)
            db.delete(sp)

    logger.info(
        "save_pipeline_run: researcher=%s → %d publications, %d citations persisted",
        researcher_dict.get("display_name"),
        saved_pubs,
        saved_citations,
    )
    return researcher_row


# ---------------------------------------------------------------------------
# Re-query stored results (no API calls)
# ---------------------------------------------------------------------------


def get_researcher_citations(
    openalex_id: str,
    db: Session,
    *,
    classification_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Re-query all stored citations for a researcher from the database.
    No external API calls are made.

    Parameters
    ----------
    openalex_id:
        The researcher's OpenAlex author ID (e.g. ``"A5086198262"``) or ORCID.
    db:
        Active SQLAlchemy session.
    classification_filter:
        Optional "SELF" | "EXTERNAL" | "UNRESOLVED" to filter results.

    Returns
    -------
    dict with keys:
        researcher     : dict  — researcher metadata
        publications   : list[dict]
        citations      : list[dict]  — flat list across all publications
        total          : int
    """
    clean_id = (openalex_id or "").replace("https://openalex.org/", "").strip()
    researcher_row = (
        db.query(DBResearcher)
        .filter(
            (DBResearcher.openalex_id == clean_id)
            | (DBResearcher.openalex_id == openalex_id)
            | (DBResearcher.orcid == openalex_id)
            | (DBResearcher.orcid == f"https://orcid.org/{clean_id}")
        )
        .first()
    )
    if researcher_row is None:
        logger.warning("get_researcher_citations: no stored data for %s", openalex_id)
        return {"researcher": None, "publications": [], "citations": [], "total": 0}

    researcher_out = {
        "openalex_id": researcher_row.openalex_id,
        "orcid": researcher_row.orcid,
        "display_name": researcher_row.name,
        "affiliation": researcher_row.affiliation,
    }

    publications_out: List[Dict[str, Any]] = []
    citations_out: List[Dict[str, Any]] = []

    for pub in researcher_row.publications:
        publications_out.append({
            "work_id": pub.work_id,
            "title": pub.title,
            "year": pub.year,
            "doi": pub.doi,
        })

        query = db.query(DBCitation).filter_by(publication_id=pub.id)
        if classification_filter:
            query = query.filter_by(classification=classification_filter)

        for cit in query.all():
            citations_out.append({
                "id": cit.id,
                "title": cit.title,
                "authors": json.loads(cit.authors or "[]"),
                "year": cit.year,
                "doi": cit.doi,
                "venue": cit.venue,
                "sources": json.loads(cit.sources or "[]"),
                "duplicate_count": cit.duplicate_count,
                "classification": cit.classification,
                "confidence": cit.confidence,
                "evidence": cit.evidence,
                "needs_review": bool(cit.needs_review),
                "retrieved_at": cit.retrieved_at.isoformat() if cit.retrieved_at else None,
                "cited_work_id": pub.work_id,
                "cited_doi": pub.doi,
            })

    logger.info(
        "get_researcher_citations: %s → %d publications, %d citations (filter=%s)",
        openalex_id,
        len(publications_out),
        len(citations_out),
        classification_filter,
    )

    return {
        "researcher": researcher_out,
        "publications": publications_out,
        "citations": citations_out,
        "total": len(citations_out),
    }


def get_all_researchers(db: Session) -> List[Dict[str, Any]]:
    """
    Return a summary list of all stored researchers, most recently updated first.
    Useful for the /researchers API endpoint.
    """
    rows = db.query(DBResearcher).order_by(DBResearcher.updated_at.desc()).all()
    return [
        {
            "openalex_id": r.openalex_id,
            "orcid": r.orcid,
            "display_name": r.name,
            "affiliation": r.affiliation,
            "publication_count": len(r.publications),
        }
        for r in rows
    ]


def delete_researcher(openalex_id: str, db: Session) -> bool:
    """
    Delete a researcher and all their publications and citations from the database.
    """
    clean_id = (openalex_id or "").replace("https://openalex.org/", "").strip()
    researcher_row = (
        db.query(DBResearcher)
        .filter(
            (DBResearcher.openalex_id == clean_id)
            | (DBResearcher.openalex_id == openalex_id)
            | (DBResearcher.orcid == openalex_id)
            | (DBResearcher.orcid == f"https://orcid.org/{clean_id}")
        )
        .first()
    )
    if not researcher_row:
        return False

    for pub in researcher_row.publications:
        db.query(DBCitation).filter_by(publication_id=pub.id).delete(synchronize_session=False)
        db.delete(pub)
    db.delete(researcher_row)
    db.commit()
    logger.info("delete_researcher: removed researcher %s and associated records", openalex_id)
    return True


def delete_all_researchers(db: Session) -> int:
    """
    Wipe all stored researchers, publications, and citations.
    """
    db.query(DBCitation).delete(synchronize_session=False)
    db.query(DBPublication).delete(synchronize_session=False)
    count = db.query(DBResearcher).delete(synchronize_session=False)
    db.commit()
    logger.info("delete_all_researchers: wiped all %d stored researcher profiles", count)
    return count
