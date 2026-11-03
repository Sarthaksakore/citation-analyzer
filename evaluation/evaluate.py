"""
evaluation/evaluate.py — Layer 6: Pipeline Evaluation.

Loads gold_standard.csv, runs it through dedup.py and disambiguation.py,
then computes full evaluation metrics using sklearn:

Deduplication metrics  (group-based)
--------------------------------------
  True Positive  = two records in the same true_duplicate_group that the
                   pipeline also merged into one unique citation
  False Positive = two records the pipeline merged that belong to DIFFERENT
                   groups (incorrect merge)
  False Negative = two records in the same true group that the pipeline
                   left as separate records

Classification metrics  (per-citation)
----------------------------------------
  Precision / Recall / F1 / Accuracy for SELF vs EXTERNAL vs UNRESOLVED
  (macro-averaged and per-class)
  Confusion matrix printed + saved as markdown table.

Usage
-----
  python evaluation/evaluate.py
  python evaluation/evaluate.py --gold evaluation/gold_standard.csv \\
                                 --researcher-name "Lokman I. Meho" \\
                                 --orcid 0000-0003-0623-4652 \\
                                 --output evaluation/results.md

Gold standard CSV columns
--------------------------
  title                   : str   — citing paper title
  authors                 : str   — pipe-separated author names  e.g. "A|B|C"
  year                    : int
  doi                     : str   — optional DOI (leave blank if unknown)
  source                  : str   — "openalex" | "crossref" | "semantic_scholar"
  true_duplicate_group_id : str   — records with the same ID are duplicates
  true_classification     : str   — "SELF" | "EXTERNAL" | "UNRESOLVED"
"""

from __future__ import annotations

import argparse
import sys
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
from sklearn.metrics import (
    ConfusionMatrixDisplay,
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)

# Force UTF-8 output on Windows so Unicode author names / symbols don't crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Add project root to path so app.* imports work when run from any directory
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.dedup import deduplicate_citations
from app.disambiguation import classify_all


# ---------------------------------------------------------------------------
# Gold standard loading
# ---------------------------------------------------------------------------

REQUIRED_GOLD_COLUMNS = {
    "title",
    "authors",
    "year",
    "doi",
    "source",
    "true_duplicate_group_id",
    "true_classification",
}


def load_gold(gold_path: Path) -> pd.DataFrame:
    """
    Load and validate the gold-standard CSV.

    Returns a DataFrame with NaN replaced by empty strings for string cols
    and 0 for numeric cols so downstream code doesn't need to handle NaN.
    """
    df = pd.read_csv(gold_path, dtype=str)
    missing = REQUIRED_GOLD_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"gold_standard.csv is missing required columns: {missing}")

    # Normalise
    df["title"] = df["title"].fillna("").str.strip()
    df["authors"] = df["authors"].fillna("").str.strip()
    df["doi"] = df["doi"].fillna("").str.strip().str.lower()
    df["source"] = df["source"].fillna("openalex").str.strip().str.lower()
    df["true_duplicate_group_id"] = df["true_duplicate_group_id"].fillna("").str.strip()
    df["true_classification"] = df["true_classification"].fillna("UNRESOLVED").str.upper().str.strip()
    df["year"] = pd.to_numeric(df["year"], errors="coerce").fillna(0).astype(int)

    # Row index for traceability
    df["_row_id"] = [f"row_{i}" for i in range(len(df))]
    return df


# ---------------------------------------------------------------------------
# Convert gold rows → list[dict] for pipeline functions
# ---------------------------------------------------------------------------


def gold_to_raw_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    Convert gold DataFrame rows into the plain dict format expected by
    deduplicate_citations() and classify_all().
    """
    records = []
    for _, row in df.iterrows():
        authors = [a.strip() for a in str(row["authors"]).split("|") if a.strip()]
        records.append({
            "title": row["title"],
            "authors": authors,
            "year": int(row["year"]) if int(row["year"]) > 0 else None,
            "doi": row["doi"] if row["doi"] else None,
            "source": row["source"],
            "source_id": None,
            "venue": None,
            "cited_work_id": None,
            "cited_doi": None,
            # _raw_id is what deduplicate_citations stores in source_record_ids
            # Set it equal to _row_id so we can trace back after dedup
            "_raw_id": row["_row_id"],
            # Keep gold metadata for tracing
            "_row_id": row["_row_id"],
            "_true_group": row["true_duplicate_group_id"],
            "_true_class": row["true_classification"],
        })
    return records


# ---------------------------------------------------------------------------
# Deduplication evaluation (group/pair-based)
# ---------------------------------------------------------------------------


def _pairs_in_group(ids: List[str]) -> List[Tuple[str, str]]:
    """Return all unique pairs from a list of IDs (sorted for stability)."""
    return [tuple(sorted(p)) for p in combinations(ids, 2)]


def evaluate_deduplication(
    raw_records: List[Dict],
    deduped: List[Dict],
) -> Dict[str, Any]:
    """
    Evaluate deduplication quality using a pair-based approach.

    For each pair of raw records we ask:
      - Are they in the same TRUE duplicate group?  (gold label)
      - Did the pipeline assign them to the same cluster?  (predicted label)

    A *cluster* here = the set of ``source_record_ids`` on each deduplicated record.

    TP: same gold group AND same predicted cluster
    FP: different gold groups AND same predicted cluster (wrong merge)
    FN: same gold group AND different predicted clusters (missed merge)
    TN: different gold groups AND different predicted clusters

    Returns metrics dict.
    """
    # Build: row_id → true group
    row_to_true_group: Dict[str, str] = {
        r["_row_id"]: r["_true_group"] for r in raw_records
    }

    # Build: row_id → predicted cluster id (index in deduped list)
    row_to_pred_cluster: Dict[str, int] = {}
    for cluster_idx, dedup_rec in enumerate(deduped):
        src_ids = dedup_rec.get("source_record_ids") or []
        # source_record_ids stores the _raw_id UUIDs; we stored _row_id in _raw_id
        for sid in src_ids:
            if sid in row_to_true_group:
                row_to_pred_cluster[sid] = cluster_idx
        # Fallback: match by _raw_id from the dedup output itself
        raw_id = dedup_rec.get("_raw_id")
        if raw_id and raw_id in row_to_true_group:
            row_to_pred_cluster[raw_id] = cluster_idx

    # If mapping is empty, try matching by title as fallback
    if not row_to_pred_cluster:
        title_to_row = {r["title"]: r["_row_id"] for r in raw_records}
        for cluster_idx, dedup_rec in enumerate(deduped):
            title = dedup_rec.get("title", "")
            if title in title_to_row:
                row_id = title_to_row[title]
                row_to_pred_cluster[row_id] = cluster_idx

    all_row_ids = [r["_row_id"] for r in raw_records]
    all_pairs = _pairs_in_group(all_row_ids)

    tp = fp = fn = tn = 0
    pair_results = []

    for r1, r2 in all_pairs:
        same_true = (row_to_true_group.get(r1) == row_to_true_group.get(r2)
                     and row_to_true_group.get(r1) != "")
        same_pred = (row_to_pred_cluster.get(r1) is not None
                     and row_to_pred_cluster.get(r1) == row_to_pred_cluster.get(r2))

        if same_true and same_pred:
            tp += 1
        elif not same_true and same_pred:
            fp += 1
        elif same_true and not same_pred:
            fn += 1
        else:
            tn += 1

        pair_results.append({
            "pair": (r1, r2),
            "same_true_group": same_true,
            "same_pred_cluster": same_pred,
        })

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1        = (2 * precision * recall / (precision + recall)
                 if (precision + recall) > 0 else 0.0)

    return {
        "component": "deduplication",
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1_score": round(f1, 4),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "total_pairs": len(all_pairs),
        "raw_records": len(raw_records),
        "unique_after_dedup": len(deduped),
        "duplicates_removed": len(raw_records) - len(deduped),
    }


# ---------------------------------------------------------------------------
# Classification evaluation
# ---------------------------------------------------------------------------


def evaluate_classification(
    raw_records: List[Dict],
    classified: List[Dict],
) -> Dict[str, Any]:
    """
    Evaluate the SELF / EXTERNAL / UNRESOLVED classification.

    Matching strategy: match each raw record's _row_id to the classified
    output via source_record_ids, then compare true vs predicted class.
    Falls back to title-based matching if IDs are unavailable.
    """
    # Build: row_id → true class from raw records
    row_to_true: Dict[str, str] = {
        r["_row_id"]: r["_true_class"] for r in raw_records
    }

    # Build: row_id → predicted class from classified output
    row_to_pred: Dict[str, str] = {}
    for rec in classified:
        pred_class = rec.get("classification", "UNRESOLVED")
        src_ids = rec.get("source_record_ids") or []
        for sid in src_ids:
            if sid in row_to_true:
                row_to_pred[sid] = pred_class
        raw_id = rec.get("_raw_id")
        if raw_id and raw_id in row_to_true:
            row_to_pred[raw_id] = pred_class

    # Fallback: title-based matching on deduplicated/classified records
    if not row_to_pred:
        title_to_row = {r["title"]: r["_row_id"] for r in raw_records}
        for rec in classified:
            title = rec.get("title", "")
            if title in title_to_row:
                row_id = title_to_row[title]
                row_to_pred[row_id] = rec.get("classification", "UNRESOLVED")

    # Build aligned y_true / y_pred vectors
    y_true, y_pred, unmatched = [], [], []
    for row_id, true_class in row_to_true.items():
        if row_id in row_to_pred:
            y_true.append(true_class)
            y_pred.append(row_to_pred[row_id])
        else:
            unmatched.append(row_id)

    if not y_true:
        return {
            "component": "classification",
            "error": "No matched rows between gold and pipeline output.",
            "unmatched": unmatched,
        }

    labels = ["SELF", "EXTERNAL", "UNRESOLVED"]
    precision = precision_score(y_true, y_pred, labels=labels, average="macro", zero_division=0)
    recall    = recall_score   (y_true, y_pred, labels=labels, average="macro", zero_division=0)
    f1        = f1_score       (y_true, y_pred, labels=labels, average="macro", zero_division=0)
    accuracy  = accuracy_score (y_true, y_pred)
    cm        = confusion_matrix(y_true, y_pred, labels=labels)
    report    = classification_report(y_true, y_pred, labels=labels, zero_division=0)

    return {
        "component": "classification",
        "precision": round(precision, 4),
        "recall":    round(recall,    4),
        "f1_score":  round(f1,        4),
        "accuracy":  round(accuracy,  4),
        "support":   len(y_true),
        "unmatched": unmatched,
        "confusion_matrix": cm.tolist(),
        "confusion_labels": labels,
        "per_class_report": report,
        "y_true": y_true,
        "y_pred": y_pred,
    }


# ---------------------------------------------------------------------------
# Markdown report builder
# ---------------------------------------------------------------------------


def _cm_to_markdown(cm: List[List[int]], labels: List[str]) -> str:
    """Render a confusion matrix as a markdown table."""
    header = "| True \\ Pred | " + " | ".join(labels) + " |"
    sep    = "|" + "|".join(["---"] * (len(labels) + 1)) + "|"
    rows = []
    for i, row_label in enumerate(labels):
        cells = " | ".join(str(cm[i][j]) for j in range(len(labels)))
        rows.append(f"| **{row_label}** | {cells} |")
    return "\n".join([header, sep] + rows)


def build_report(
    gold_path: Path,
    dedup_result: Dict,
    cls_result: Dict,
    researcher_name: str,
) -> str:
    """Return the full evaluation report as a markdown string."""
    lines = [
        "# Citation Analyzer — Layer 6 Evaluation Report",
        "",
        f"**Gold standard**: `{gold_path.name}`  ",
        f"**Target researcher**: {researcher_name}  ",
        "",
        "---",
        "",
        "## 1. Deduplication",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Precision | {dedup_result['precision']:.4f} |",
        f"| Recall    | {dedup_result['recall']:.4f} |",
        f"| F1 Score  | {dedup_result['f1_score']:.4f} |",
        f"| TP / FP / FN / TN | {dedup_result['tp']} / {dedup_result['fp']} / {dedup_result['fn']} / {dedup_result['tn']} |",
        f"| Raw records | {dedup_result['raw_records']} |",
        f"| After dedup | {dedup_result['unique_after_dedup']} |",
        f"| Duplicates removed | {dedup_result['duplicates_removed']} |",
        "",
        "---",
        "",
        "## 2. Classification (SELF / EXTERNAL / UNRESOLVED)",
        "",
    ]

    if "error" in cls_result:
        lines.append(f"> ⚠ {cls_result['error']}")
    else:
        lines += [
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Precision (macro) | {cls_result['precision']:.4f} |",
            f"| Recall (macro)    | {cls_result['recall']:.4f} |",
            f"| F1 Score (macro)  | {cls_result['f1_score']:.4f} |",
            f"| Accuracy          | {cls_result['accuracy']:.4f} |",
            f"| Support           | {cls_result['support']} rows matched |",
            "",
            "### Confusion Matrix",
            "",
            _cm_to_markdown(cls_result["confusion_matrix"], cls_result["confusion_labels"]),
            "",
            "### Per-class Report",
            "",
            "```",
            cls_result["per_class_report"],
            "```",
        ]

        if cls_result.get("unmatched"):
            lines += [
                "",
                f"> ⚠ {len(cls_result['unmatched'])} gold row(s) could not be matched "
                f"to pipeline output: {cls_result['unmatched']}",
            ]

    lines += [
        "",
        "---",
        "",
        "> *Generated by `evaluation/evaluate.py` — Layer 6 of the Citation Analyzer pipeline.*",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------


def print_summary(dedup_result: Dict, cls_result: Dict) -> None:
    sep = "=" * 60
    print(f"\n{sep}")
    print("  Citation Analyzer — Layer 6 Evaluation")
    print(sep)

    print("\n[1] DEDUPLICATION")
    print(f"    Precision : {dedup_result['precision']:.4f}")
    print(f"    Recall    : {dedup_result['recall']:.4f}")
    print(f"    F1 Score  : {dedup_result['f1_score']:.4f}")
    print(f"    TP={dedup_result['tp']}  FP={dedup_result['fp']}  "
          f"FN={dedup_result['fn']}  TN={dedup_result['tn']}")
    print(f"    {dedup_result['raw_records']} raw -> "
          f"{dedup_result['unique_after_dedup']} unique "
          f"({dedup_result['duplicates_removed']} removed)")

    print("\n[2] CLASSIFICATION")
    if "error" in cls_result:
        print(f"    ERROR: {cls_result['error']}")
    else:
        print(f"    Precision : {cls_result['precision']:.4f}")
        print(f"    Recall    : {cls_result['recall']:.4f}")
        print(f"    F1 Score  : {cls_result['f1_score']:.4f}")
        print(f"    Accuracy  : {cls_result['accuracy']:.4f}")
        print(f"    Support   : {cls_result['support']} matched rows")
        print()
        print(cls_result["per_class_report"])

    print(sep)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: List[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Layer 6: evaluate deduplication + classification against gold standard."
    )
    parser.add_argument(
        "--gold",
        type=Path,
        default=Path(__file__).parent / "gold_standard.csv",
        help="Path to gold_standard.csv (default: evaluation/gold_standard.csv)",
    )
    parser.add_argument(
        "--researcher-name",
        default="Lokman I. Meho",
        help="Display name of the target researcher for disambiguation",
    )
    parser.add_argument(
        "--orcid",
        default=None,
        help="ORCID of the target researcher (improves classification accuracy)",
    )
    parser.add_argument(
        "--affiliation",
        default=None,
        help="Institution of the target researcher",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "results.md",
        help="Output path for the markdown report (default: evaluation/results.md)",
    )
    args = parser.parse_args(argv)

    # ── Load gold ───────────────────────────────────────────────────────────
    if not args.gold.exists():
        print(f"ERROR: gold standard not found: {args.gold}", file=sys.stderr)
        sys.exit(1)

    gold_df = load_gold(args.gold)
    print(f"Loaded {len(gold_df)} gold rows from {args.gold.name}")

    raw_records = gold_to_raw_records(gold_df)

    # ── Build researcher dict for Layer 4 ──────────────────────────────────
    researcher = {
        "display_name": args.researcher_name,
        "orcid": args.orcid,
        "openalex_id": None,
        "affiliation": args.affiliation,
        "known_coauthors": [],
    }

    # ── Layer 3: Deduplicate ────────────────────────────────────────────────
    print("Running deduplication...")
    deduped = deduplicate_citations(raw_records)

    # ── Layer 4: Classify ───────────────────────────────────────────────────
    print("Running disambiguation / classification...")
    classified = classify_all(deduped, researcher)

    # ── Evaluate ────────────────────────────────────────────────────────────
    print("Computing metrics...")
    dedup_result = evaluate_deduplication(raw_records, deduped)
    cls_result   = evaluate_classification(raw_records, classified)

    # ── Print summary ───────────────────────────────────────────────────────
    print_summary(dedup_result, cls_result)

    # ── Save markdown report ────────────────────────────────────────────────
    report_md = build_report(args.gold, dedup_result, cls_result, args.researcher_name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report_md, encoding="utf-8")
    print(f"\nReport saved to: {args.output}")


if __name__ == "__main__":
    main()
