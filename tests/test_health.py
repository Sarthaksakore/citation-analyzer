"""
tests/test_health.py — Smoke test: verify the FastAPI app starts and /health returns 200.
"""

# pyrefly: ignore [missing-import]
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "citation-analyzer"


def test_root_redirect():
    response = client.get("/")
    assert response.status_code == 200
    assert "message" in response.json()
