"""Illustrative sketch: a chatty financial-data explorer using the Claude
Agent SDK pattern (anthropic.Anthropic.messages with tool_use blocks).

THIS IS NOT A RUNNABLE PROGRAM TODAY. It exists as reference material for
the architectural comparison in ``README.md`` and ``docs/decisions/ADR-006``.

To ship this for real:

1. Replace ``_TOOL_DEFINITIONS`` with bindings to ``src/tools/api.py`` — each
   function becomes a tool with a JSON-Schema input.
2. Implement ``_dispatch_tool_use`` to actually call the bound function and
   return a JSON-serialisable result.
3. Plumb your API key via env var or your secrets manager of choice.
4. Wrap the loop in a CLI entry point (Click / Typer / argparse).
5. Add tests with ``respx`` mocking the Anthropic HTTP calls, matching the
   project's existing test conventions.

The structure below is correct for the SDK pattern; only the tool plumbing
and the entry point are missing.
"""

from __future__ import annotations

import json
from typing import Any

# Tool definitions follow the Anthropic Messages-API ``tool_use`` shape.
# In a real implementation each ``input_schema`` is generated from the
# corresponding function in ``src/tools/api.py`` (Pydantic-derivable).
_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_price_history",
        "description": (
            "Fetch daily OHLCV price history for a ticker between two dates. "
            "Use when the user asks about price action, returns, or volatility."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "e.g. AAPL"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["ticker", "start_date", "end_date"],
        },
    },
    {
        "name": "get_fundamentals",
        "description": (
            "Fetch the latest reported fundamentals (revenue, earnings, "
            "margins, balance sheet snapshot) for a ticker."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_recent_news_sentiment",
        "description": (
            "Fetch a sentiment summary of recent news for a ticker. "
            "Use for post-earnings reaction questions or event-driven inquiries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "days": {"type": "integer", "minimum": 1, "maximum": 60},
            },
            "required": ["ticker"],
        },
    },
]


_SYSTEM_PROMPT = """\
You are a financial-data research assistant. You answer questions about
publicly-traded companies using the tools provided. You always cite which
tool you used and the timestamp of the data. You do not make recommendations
to buy or sell — you provide data and analysis. If a tool fails, say so
clearly. If you don't have a tool for what's asked, say so clearly.
"""


def _dispatch_tool_use(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool by name and return its result.

    In the sketch this is a stub; in a real build, each name dispatches to
    the matching function in ``src/tools/api.py`` and returns the result as
    a JSON-serialisable dict.
    """
    # NOTE: stub — real implementation calls into src/tools/api.py
    return {"status": "stub", "tool": name, "echo_input": tool_input}


def run_one_turn(
    client: Any,  # anthropic.Anthropic instance — left as Any to keep this file dep-free
    messages: list[dict[str, Any]],
    *,
    model: str = "claude-3-5-sonnet-latest",
    max_tokens: int = 4096,
) -> list[dict[str, Any]]:
    """Run one turn of the conversation. Returns the updated message list.

    A real CLI loop would call this repeatedly:

        messages = []
        while True:
            user = input("> ")
            messages.append({"role": "user", "content": user})
            messages = run_one_turn(client, messages)
            print(messages[-1]["content"])  # last assistant turn
    """
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        tools=_TOOL_DEFINITIONS,
        messages=messages,
    )
    # Append the assistant message (may include tool_use blocks).
    messages.append({"role": "assistant", "content": response.content})

    # If Claude requested tool calls, execute and feed results back.
    tool_uses = [block for block in response.content if getattr(block, "type", None) == "tool_use"]
    if not tool_uses:
        return messages

    tool_results = []
    for tool_use in tool_uses:
        result = _dispatch_tool_use(tool_use.name, tool_use.input)
        tool_results.append(
            {
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": json.dumps(result),
            }
        )
    messages.append({"role": "user", "content": tool_results})

    # Recursive single step: let Claude produce its natural-language summary
    # of the tool results. In a real loop, you might cap recursion depth.
    return run_one_turn(client, messages, model=model, max_tokens=max_tokens)
