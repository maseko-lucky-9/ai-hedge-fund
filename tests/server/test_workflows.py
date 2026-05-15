"""Validation + happy-path tests for ``/api/workflows/earnings-reaction/run``.

These do not invoke real LLMs. The success-path tests monkeypatch the service
entry point (the same pattern ``test_runs_validation.py`` uses) so the route
exercises validation + response shaping without external dependencies and
without tripping the pre-existing ``datetime.utcnow`` DeprecationWarning in
``server/db/models.py:21`` that fires when the persistence path runs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

_BASE_BODY = {
    "tickers": ["AAPL"],
    "model_name": "claude-3-5-sonnet-latest",
    "model_provider": "Anthropic",
}


def test_rejects_invalid_ticker(client: TestClient) -> None:
    body = {**_BASE_BODY, "tickers": ["AAPL\nBREAKOUT"]}
    response = client.post("/api/workflows/earnings-reaction/run", json=body)
    assert response.status_code == 422
    assert "invalid ticker symbol" in response.text


def test_rejects_inverted_date_range(client: TestClient) -> None:
    body = {**_BASE_BODY, "start_date": "2026-12-31", "end_date": "2026-01-01"}
    response = client.post("/api/workflows/earnings-reaction/run", json=body)
    assert response.status_code == 422


def test_rejects_empty_tickers(client: TestClient) -> None:
    body = {**_BASE_BODY, "tickers": []}
    response = client.post("/api/workflows/earnings-reaction/run", json=body)
    assert response.status_code == 422


def test_happy_path_returns_201_with_fixed_analyst_lineup(client: TestClient, monkeypatch) -> None:
    """Stub the service entry point and confirm the route returns a 201
    RunSummary whose config.selected_analysts is the fixed Earnings Reaction
    lineup regardless of what the request body said."""
    from server.schemas import Decision, RunSummary
    from server.services import earnings_reaction as svc
    from src.workflows.earnings_reaction import EARNINGS_REACTION_ANALYSTS

    async def _fake_execute(req, session):  # noqa: ARG001
        # Mimic the real service's response: fixed analyst lineup, parsed
        # decisions, populated signals.
        return RunSummary(
            id="stub-run-1",
            status="done",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            duration_ms=1234,
            config=req.model_copy(update={"selected_analysts": list(EARNINGS_REACTION_ANALYSTS)}),
            decisions={
                "AAPL": Decision(action="buy", quantity=5, confidence=78.0, reasoning="post-earnings re-rating")
            },
            analyst_signals={},
        )

    monkeypatch.setattr(svc, "execute_earnings_reaction_run", _fake_execute)
    monkeypatch.setattr(
        "server.api.workflows.execute_earnings_reaction_run",
        svc.execute_earnings_reaction_run,
    )

    body = {
        **_BASE_BODY,
        # User attempt to override analyst lineup — must be ignored.
        "selected_analysts": ["warren_buffett"],
    }
    response = client.post("/api/workflows/earnings-reaction/run", json=body)

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["status"] == "done"
    assert payload["config"]["selected_analysts"] == list(EARNINGS_REACTION_ANALYSTS)
    assert payload["decisions"]["AAPL"]["action"] == "buy"
    assert payload["decisions"]["AAPL"]["quantity"] == 5


def test_failure_path_propagates_exception(client: TestClient, monkeypatch) -> None:
    """When the service raises, the exception bubbles out of the route.

    The repo's FastAPI app does not register a global exception handler, so
    in production this surfaces as a 500 via Starlette's default handler. The
    ``TestClient`` re-raises by default (``raise_server_exceptions=True``), so
    the assertion is that the route does not swallow the error.
    """
    import pytest
    from server.services import earnings_reaction as svc

    async def _explode(req, session):  # noqa: ARG001
        raise RuntimeError("model API unavailable")

    monkeypatch.setattr(svc, "execute_earnings_reaction_run", _explode)
    monkeypatch.setattr(
        "server.api.workflows.execute_earnings_reaction_run",
        svc.execute_earnings_reaction_run,
    )

    with pytest.raises(RuntimeError, match="model API unavailable"):
        client.post("/api/workflows/earnings-reaction/run", json=_BASE_BODY)


def test_thread_id_is_accepted_in_payload(client: TestClient, monkeypatch) -> None:
    """The optional thread_id field round-trips through the request schema."""
    captured = {}

    from server.schemas import RunSummary
    from server.services import earnings_reaction as svc
    from src.workflows.earnings_reaction import EARNINGS_REACTION_ANALYSTS

    async def _capture(req, session):  # noqa: ARG001
        captured["thread_id"] = req.thread_id
        return RunSummary(
            id="stub-thread",
            status="done",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            duration_ms=1,
            config=req.model_copy(update={"selected_analysts": list(EARNINGS_REACTION_ANALYSTS)}),
        )

    monkeypatch.setattr(svc, "execute_earnings_reaction_run", _capture)
    monkeypatch.setattr(
        "server.api.workflows.execute_earnings_reaction_run",
        svc.execute_earnings_reaction_run,
    )

    body = {**_BASE_BODY, "thread_id": "my-resumable-run-42"}
    response = client.post("/api/workflows/earnings-reaction/run", json=body)

    assert response.status_code == 201, response.text
    assert captured["thread_id"] == "my-resumable-run-42"


def test_health_and_existing_routes_still_register(client: TestClient) -> None:
    """Regression guard: mounting the new workflows router must not displace
    the existing health/runs routers."""
    health = client.get("/api/healthz")
    assert health.status_code == 200


def test_openapi_lists_workflow_route(client: TestClient) -> None:
    schema = client.get("/api/openapi.json").json()
    assert "/api/workflows/earnings-reaction/run" in schema["paths"]
    assert "post" in schema["paths"]["/api/workflows/earnings-reaction/run"]
