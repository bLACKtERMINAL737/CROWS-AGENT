from fastapi.testclient import TestClient

import crows.app as app_module


def test_run_coding_tests_sanitizes_unexpected_errors(monkeypatch):
    class BrokenCoding:
        def run_tests(self):
            raise OSError("private path /Users/private/.env token=secret-value")

    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(app_module, "coding", BrokenCoding())
    response = TestClient(app_module.app, raise_server_exceptions=False).post(
        "/coding/run-tests",
        headers={"X-Aether-Token": "test-token"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Coding test run failed; inspect local logs"
    assert "private path" not in response.text
    assert "/Users/private" not in response.text
    assert "secret-value" not in response.text


def test_run_coding_tests_preserves_expected_coding_errors(monkeypatch):
    from crows.coding import CodingError

    class InvalidCoding:
        def run_tests(self):
            raise CodingError("Docker test runner is disabled")

    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(app_module, "coding", InvalidCoding())
    response = TestClient(app_module.app, raise_server_exceptions=False).post(
        "/coding/run-tests",
        headers={"X-Aether-Token": "test-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Docker test runner is disabled"
