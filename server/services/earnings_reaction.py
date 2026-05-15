"""Earnings Reaction Playbook run service.

Wraps :func:`src.workflows.build_earnings_reaction_graph` for the FastAPI
route, persists the run to the same ``runs`` table the ``/api/runs`` endpoint
uses, and returns a :class:`RunSummary` matching the existing contract.

The CLI ``progress`` singleton is left untouched here — the streaming variant
(future work) will introduce a ContextVar-scoped sink. This sync path is
focused on demo-grade correctness and persistence.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Repo's ``src/`` on sys.path — same shim pattern as ``server/services/run_service.py``.
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from src.workflows.earnings_reaction import (  # noqa: E402
    EARNINGS_REACTION_ANALYSTS,
    build_earnings_reaction_graph,
    earnings_reaction_initial_state,
    memory_checkpointer,
)

from ..db.models import Run, RunDecision, RunSignal  # noqa: E402
from ..schemas import (  # noqa: E402
    AnalystSignal,
    Decision,
    EarningsReactionRequest,
    RunSummary,
)

if TYPE_CHECKING:
    from sqlmodel import Session


_WORKFLOW_KIND = "analyze"  # runs table only knows analyze|backtest today;
# treat the playbook as an analyze-flavoured run with metadata pinning it to
# the workflow. A future migration adds "workflow" as a first-class kind.


def _portfolio_for_graph(req: EarningsReactionRequest) -> dict[str, Any]:
    """Project the request portfolio into the dict shape the agent stack
    expects (mirrors ``run_service._portfolio_for_cli``)."""
    return {
        "cash": req.portfolio.cash,
        "margin_requirement": req.portfolio.margin_requirement,
        "positions": {
            t: {
                "long": p.long,
                "short": p.short,
                "long_cost_basis": p.long_cost_basis,
                "short_cost_basis": p.short_cost_basis,
            }
            for t, p in req.portfolio.positions.items()
        },
        "realized_gains": req.portfolio.realized_gains,
    }


def _parse_decisions(raw: Any) -> dict[str, Decision]:
    """Best-effort decode of the portfolio manager's final message content.

    Mirrors ``src.main.parse_hedge_fund_response`` semantics but returns
    typed :class:`Decision` objects keyed by ticker. Returns an empty dict on
    any parse failure — the caller still gets a usable RunSummary."""
    import json

    if raw is None:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}

    # Portfolio manager produces {"decisions": {TICKER: {...}}}
    container = parsed.get("decisions", parsed)
    if not isinstance(container, dict):
        return {}

    out: dict[str, Decision] = {}
    for ticker, payload in container.items():
        if not isinstance(payload, dict):
            continue
        try:
            out[ticker] = Decision(
                action=payload.get("action", "hold"),
                quantity=int(payload.get("quantity", 0)),
                confidence=float(payload.get("confidence", 0.0)),
                reasoning=payload.get("reasoning"),
            )
        except (ValueError, TypeError):
            continue
    return out


def _signals_from_state(analyst_signals: dict[str, Any]) -> dict[str, dict[str, AnalystSignal]]:
    """Project the agent_signals nested dict into the typed shape RunSummary expects."""
    out: dict[str, dict[str, AnalystSignal]] = {}
    if not isinstance(analyst_signals, dict):
        return out
    for agent_name, per_ticker in analyst_signals.items():
        if not isinstance(per_ticker, dict):
            continue
        out[agent_name] = {}
        for ticker, payload in per_ticker.items():
            if not isinstance(payload, dict):
                continue
            try:
                out[agent_name][ticker] = AnalystSignal(
                    **{
                        k: v
                        for k, v in payload.items()
                        if k in {"signal", "confidence", "reasoning", "remaining_position_limit", "current_price"}
                    }
                )
            except (ValueError, TypeError):
                continue
    return out


def _run_graph_sync(state: dict[str, Any], thread_id: str) -> dict[str, Any]:
    """Build the workflow and invoke it. Returns the final state.

    Isolated so tests can monkeypatch it.
    """
    checkpointer = memory_checkpointer()
    graph = build_earnings_reaction_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": thread_id}}
    return graph.invoke(state, config=config)


async def execute_earnings_reaction_run(
    req: EarningsReactionRequest,
    session: Session,
) -> RunSummary:
    """Execute the Earnings Reaction Playbook end-to-end and persist the run."""
    started_at = datetime.now(UTC)
    thread_id = req.thread_id or f"earnings-reaction-{uuid.uuid4().hex[:12]}"

    run = Run(
        kind=_WORKFLOW_KIND,
        status="running",
        started_at=started_at,
        model_name=req.model_name,
        model_provider=req.model_provider,
        tickers=req.tickers,
        start_date=req.start_date or "",
        end_date=req.end_date or "",
        initial_cash=req.portfolio.cash,
        margin_requirement=req.portfolio.margin_requirement,
        # The playbook fixes the analyst lineup; record what actually ran.
        selected_analysts=list(EARNINGS_REACTION_ANALYSTS),
        show_reasoning=req.show_reasoning,
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    initial_state = earnings_reaction_initial_state(
        tickers=req.tickers,
        start_date=req.start_date or "",
        end_date=req.end_date or "",
        portfolio=_portfolio_for_graph(req),
        model_name=req.model_name,
        model_provider=req.model_provider,
        show_reasoning=req.show_reasoning,
    )

    t0 = time.monotonic()
    try:
        # Synchronous LangGraph + LLM calls — run in worker thread so the
        # FastAPI event loop stays responsive.
        final_state = await asyncio.to_thread(_run_graph_sync, initial_state, thread_id)
    except Exception as exc:  # noqa: BLE001
        run.status = "error"
        run.completed_at = datetime.now(UTC)
        run.duration_ms = int((time.monotonic() - t0) * 1000)
        run.error_message = repr(exc)
        session.add(run)
        session.commit()
        session.refresh(run)
        raise

    duration_ms = int((time.monotonic() - t0) * 1000)
    completed_at = datetime.now(UTC)

    # Persist results to the runs DB so /api/runs/{id} can return this run.
    final_messages = final_state.get("messages", [])
    final_content = final_messages[-1].content if final_messages else None
    decisions = _parse_decisions(final_content)
    signals = _signals_from_state(final_state.get("data", {}).get("analyst_signals", {}))

    for ticker, decision in decisions.items():
        session.add(
            RunDecision(
                run_id=run.id,
                ticker=ticker,
                action=decision.action,
                quantity=decision.quantity,
                confidence=decision.confidence,
                reasoning=decision.reasoning,
            )
        )
    for agent_name, per_ticker in signals.items():
        for ticker, sig in per_ticker.items():
            reasoning = sig.reasoning
            if isinstance(reasoning, dict):
                import json as _json

                reasoning = _json.dumps(reasoning)
            session.add(
                RunSignal(
                    run_id=run.id,
                    agent_name=agent_name,
                    ticker=ticker,
                    signal=sig.signal,
                    confidence=sig.confidence,
                    reasoning=reasoning,
                )
            )

    run.status = "done"
    run.completed_at = completed_at
    run.duration_ms = duration_ms
    session.add(run)
    session.commit()
    session.refresh(run)

    # Copy req and overwrite selected_analysts so the response reflects the
    # fixed playbook lineup (the request payload may have been empty).
    config_echo = req.model_copy(update={"selected_analysts": list(EARNINGS_REACTION_ANALYSTS)})

    return RunSummary(
        id=run.id,
        status=run.status,  # type: ignore[arg-type]
        started_at=run.started_at or started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        error_message=None,
        cost_usd=None,
        config=config_echo,
        decisions=decisions,
        analyst_signals=signals,
    )
