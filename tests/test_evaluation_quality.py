from pathlib import Path

import pandas as pd

from evaluation.evaluate import assess_gold_quality, load_gold


def test_current_gold_set_reports_small_sample_and_missing_doi():
    quality = assess_gold_quality(load_gold(Path("evaluation/gold_standard.csv")))

    assert quality["row_count"] == 5
    assert quality["missing_dois"] == 1
    assert any("fewer than 30" in warning for warning in quality["warnings"])
    assert any("no DOI" in warning for warning in quality["warnings"])


def test_gold_quality_rejects_invalid_labels_and_missing_titles():
    frame = pd.DataFrame([
        {
            "title": "",
            "authors": "Author",
            "year": 2020,
            "doi": "10.1000/example",
            "source": "unknown",
            "true_duplicate_group_id": "G1",
            "true_classification": "OTHER",
        }
    ])

    quality = assess_gold_quality(frame)

    assert quality["usable"] is False
    assert quality["missing_titles"] == 1
    assert quality["warnings"]


def test_current_fixture_is_not_production_ready():
    quality = assess_gold_quality(load_gold(Path("evaluation/gold_standard.csv")))

    assert quality["production_ready"] is False
