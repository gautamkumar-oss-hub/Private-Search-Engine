from fastapi.testclient import TestClient

from app import main


def test_health_and_search(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test.db")
    main.upsert_document("https://example.test", "Privacy guide", "Private local search without tracking")
    client = TestClient(main.app)
    assert client.get("/health").json()["status"] == "ok"
    response = client.post("/api/search", json={"q": "privacy tracking"})
    assert response.status_code == 200
    assert response.json()["results"][0]["title"] == "Privacy guide"


def test_empty_search_is_rejected():
    client = TestClient(main.app)
    assert client.post("/api/search", json={"q": ""}).status_code == 422
