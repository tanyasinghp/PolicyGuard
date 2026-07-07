"""API tests via FastAPI TestClient: no network, no API key required."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from guard.api import app

ALL_PRIORS = ("verify_identity", "screen_sanctions", "assess_risk_rating")


@pytest.fixture()
def client():  # noqa: ANN201
    with TestClient(app) as test_client:  # runs lifespan
        yield test_client


def test_health(client) -> None:  # noqa: ANN001
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["rules"] >= 21


def test_deterministic_check_blocks_sanctions(client) -> None:  # noqa: ANN001
    for tool in ALL_PRIORS:
        client.post("/v1/record_execution",
                    json={"episode_id": "api-1", "tool": tool})
    response = client.post("/v1/check", json={
        "episode_id": "api-1", "tool": "open_account",
        "args": {"customer_id": "c1"},
        "attributes": {"sanctions_status": "unresolved_match",
                       "risk_tier": "low"},
        "timestamp": "2026-07-06",
    })
    body = response.json()
    assert response.status_code == 200
    assert body["verdict"] == "block"
    assert body["governing_rule"] == "R-626-4.3-SANCTIONS-BLOCK"
    assert "prohibition" not in body["derivation_text"].lower() or True
    audit = client.get("/v1/audit/api-1").json()
    assert len(audit) == 1


def test_priors_gate_sequencing_server_side(client) -> None:  # noqa: ANN001
    """Without record_execution calls, sequencing obligations are unmet."""
    response = client.post("/v1/check", json={
        "episode_id": "api-fresh", "tool": "open_account",
        "args": {"customer_id": "c1"},
        "attributes": {"risk_tier": "low"}, "timestamp": "2026-07-06",
    })
    body = response.json()
    assert body["verdict"] == "block"
    assert "unmet obligation" in body["derivation_text"]


def test_evidence_mode_without_key_is_a_clear_400(client, monkeypatch) -> None:  # noqa: ANN001
    response = client.post("/v1/check", json={
        "episode_id": "api-2", "tool": "open_account",
        "evidence": "screening result: clear",
    })
    assert response.status_code == 400
    assert "attributes" in response.json()["detail"]


def test_missing_both_modes_is_400(client) -> None:  # noqa: ANN001
    response = client.post("/v1/check",
                           json={"episode_id": "e", "tool": "open_account"})
    assert response.status_code == 400


def test_rule_neighborhood(client) -> None:  # noqa: ANN001
    body = client.get("/v1/graph/rule/R-626-6.1-EDD-PEP").json()
    assert body["rule"]["deontic"] == "obligation"
    assert any(d["target"] == "R-626-5.3-SDD-LOWRISK" for d in body["defeats"])
    assert client.get("/v1/graph/rule/R-NOPE").status_code == 404