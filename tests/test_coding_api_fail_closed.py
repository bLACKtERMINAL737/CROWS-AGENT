from fastapi.testclient import TestClient

import crows.app as app_module


def test_approve_coding_proposal_hides_unexpected_exception_details(monkeypatch):
    class BrokenCoding:
        def approve(self, proposal_id):
            raise OSError("private path /Users/private/.env api_key=secret-value")

    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(app_module, "coding", BrokenCoding())
    response = TestClient(app_module.app, raise_server_exceptions=False).post(
        "/coding/proposals/proposal-1/approve", headers={"X-Aether-Token": "test-token"}
    )

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "Coding proposal approval failed; inspect local logs"
    )
    assert "private path" not in response.text
    assert "/Users/private" not in response.text
    assert "secret-value" not in response.text


def test_reject_coding_proposal_hides_unexpected_exception_details(monkeypatch):
    class BrokenCoding:
        def reject(self, proposal_id):
            raise OSError("private path /Users/private/.env api_key=secret-value")

    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(app_module, "coding", BrokenCoding())
    response = TestClient(app_module.app, raise_server_exceptions=False).post(
        "/coding/proposals/proposal-2/reject", headers={"X-Aether-Token": "test-token"}
    )

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "Coding proposal rejection failed; inspect local logs"
    )
    assert "private path" not in response.text
    assert "/Users/private" not in response.text
    assert "secret-value" not in response.text


def test_approve_coding_proposal_preserves_expected_http_errors(monkeypatch):
    class MissingCoding:
        def approve(self, proposal_id):
            raise KeyError("Proposal not found")

    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(app_module, "coding", MissingCoding())
    response = TestClient(app_module.app, raise_server_exceptions=False).post(
        "/coding/proposals/missing/approve", headers={"X-Aether-Token": "test-token"}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "'Proposal not found'"


def test_reject_coding_proposal_preserves_expected_conflicts(monkeypatch):
    from crows.coding import CodingError

    class ConflictCoding:
        def reject(self, proposal_id):
            raise CodingError("Only pending proposals can be rejected")

    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(app_module, "coding", ConflictCoding())
    response = TestClient(app_module.app, raise_server_exceptions=False).post(
        "/coding/proposals/proposal-3/reject", headers={"X-Aether-Token": "test-token"}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Only pending proposals can be rejected"
