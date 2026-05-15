"""Named-workflow endpoints.

Currently exposes the Phase 2 **Earnings Reaction Playbook** as
``POST /api/workflows/earnings-reaction/run``. See
``docs/decisions/ADR-006-earnings-reaction-playbook-and-claude-sdk-comparison.md``
for the rationale and ``docs/sales-demo.md`` for the live-demo script.

Future named workflows (e.g. "Macro Regime Shift Playbook") attach here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlmodel import Session  # noqa: TC002 — FastAPI resolves type hints at runtime via Depends

from ..db.session import get_session
from ..schemas import EarningsReactionRequest, RunSummary
from ..services.earnings_reaction import execute_earnings_reaction_run

router = APIRouter(prefix="/workflows", tags=["workflows"])


@router.post(
    "/earnings-reaction/run",
    response_model=RunSummary,
    status_code=status.HTTP_201_CREATED,
)
async def run_earnings_reaction(
    req: EarningsReactionRequest,
    session: Session = Depends(get_session),
) -> RunSummary:
    """Run the Earnings Reaction Playbook on the supplied tickers.

    The playbook is a fixed-shape LangGraph workflow: six curated analyst
    lenses (fundamentals, sentiment, technicals, valuation, Warren Buffett,
    Michael Burry) run in parallel, then risk and portfolio managers
    consolidate.

    The ``selected_analysts`` field of the request is ignored — the playbook
    fixes its lineup. The response echoes back the fixed lineup in
    ``config.selected_analysts`` for parity with ``/api/runs``.
    """
    return await execute_earnings_reaction_run(req, session)
