from app import google_scholar


def test_quota_error_is_detected():
    assert google_scholar._is_quota_error("Your account has run out of searches")
    assert google_scholar._is_quota_error("search quota exhausted")
    assert not google_scholar._is_quota_error("temporary upstream failure")


def test_fetch_all_results_consumes_every_page(monkeypatch):
    calls = []

    def fake_request(params):
        calls.append(params)
        start = params.get("start", 0)
        if start == 0:
            return {
                "organic_results": [{"result_id": "a"}, {"result_id": "b"}],
                "serpapi_pagination": {"next": "page-2"},
            }
        return {
            "organic_results": [{"result_id": "c"}],
            "serpapi_pagination": {},
        }

    monkeypatch.setattr(google_scholar, "_request", fake_request)

    results, report = google_scholar._fetch_all_results(
        {"engine": "google_scholar", "cites": "cluster"},
        "organic_results",
        page_size=100,
    )

    assert [result["result_id"] for result in results] == ["a", "b", "c"]
    assert report == {
        "pages_fetched": 2,
        "results_fetched": 3,
        "complete": True,
        "stop_reason": "no_next_page",
    }
    assert calls == [
        {"engine": "google_scholar", "cites": "cluster", "start": 0, "num": 100},
        {"engine": "google_scholar", "cites": "cluster", "start": 2, "num": 100},
    ]
