# Claude Agent SDK — comparison example

This directory exists to **demonstrate the architectural choice** captured in [`docs/decisions/ADR-006`](../../docs/decisions/ADR-006-earnings-reaction-playbook-and-claude-sdk-comparison.md): **when LangGraph is the right tool, and when Claude Agent SDK is the right tool**. It does not replace anything in `src/workflows/`.

## TL;DR

| | LangGraph (Earnings Reaction Playbook) | Claude Agent SDK (this example) |
|---|---|---|
| Shape | Fixed DAG known up front | Dynamic — agent picks tools per turn |
| State | Shared `AgentState` across nodes | Conversation history; no shared structured state |
| Persistence | `SqliteSaver` checkpoint per node | None by default; conversation transcript only |
| Reasoning model | Each node calls its own LLM | One model orchestrates a single conversation |
| Best for | Workflows that finish unattended; production durability; audit | Exploratory chat; one-off questions; user-driven inquiry |
| Worst for | Open-ended exploration | Multi-step automations; checkpointable workflows |

## The example: a chatty financial-data explorer

`financial_data_explorer.py` is an **illustrative sketch** (not a runnable demo today) that shows the Claude Agent SDK pattern: define tools, hand them to Claude, let Claude decide which to call per turn, loop until the user says stop.

If you wanted to ship this for real, you'd:

1. Wire each function in `src/tools/api.py` (price history, fundamentals, sentiment, etc.) as a tool the SDK can invoke.
2. Give Claude a system prompt scoping its role to financial-data Q&A.
3. Loop on stdin/stdout (CLI) or wire the same loop behind a FastAPI WebSocket for the web client.
4. Keep the conversation transcript in memory or a lightweight store.

The work is straightforward; the **decision** to use the SDK here instead of LangGraph is the interesting part.

## When you'd reach for this in a Prudentia engagement

- A prospect asks: *"Can we have an internal chat agent that researches any company on demand using our internal data?"* — Claude Agent SDK. The user drives the agenda turn by turn; there's no fixed multi-step playbook to run unattended.
- A prospect asks: *"Can we auto-run a 10-step due-diligence workflow every Friday and email the result?"* — LangGraph. The workflow is the product; the user is not in the loop turn by turn.

The two co-exist comfortably. The Earnings Reaction Playbook in `src/workflows/` shows the LangGraph side; this example shows the SDK side. The sales conversation uses both to qualify which entry point fits the prospect's problem.

## Side-by-side: the same business question, two architectures

### LangGraph way (existing — `src/workflows/earnings_reaction.py`)

The user picks a ticker, hits run, and walks away. Six analyst lenses run in parallel, risk consolidates, portfolio manager decides. State checkpoints to disk; the run can resume from a crash. The user inspects the result when it's done.

```python
from src.workflows import build_earnings_reaction_graph, earnings_reaction_initial_state, sqlite_checkpointer

with sqlite_checkpointer("./data/earnings_reaction.db") as saver:
    graph = build_earnings_reaction_graph(checkpointer=saver)
    state = earnings_reaction_initial_state(tickers=["AAPL"], start_date="2026-02-01", end_date="2026-05-15", portfolio={...})
    final = graph.invoke(state, config={"configurable": {"thread_id": "run-001"}})
    print(final["messages"][-1].content)  # portfolio decision
```

### Claude Agent SDK way (this example, sketched)

The user sits at a prompt and asks open-ended questions. Claude decides which tool to call. The conversation continues until the user is done.

```python
# Sketch only — see financial_data_explorer.py
client = Anthropic()
tools = build_financial_data_tools()       # exposes tools/api.py as tool defs
messages = []

while True:
    user_input = input("> ")
    if user_input.lower() in ("quit", "exit"):
        break
    messages.append({"role": "user", "content": user_input})
    response = client.messages.create(
        model="claude-3-5-sonnet-latest",
        tools=tools,
        messages=messages,
        max_tokens=4096,
    )
    messages.append({"role": "assistant", "content": response.content})
    # If response has tool_use blocks, execute, append tool_result, loop.
    # If response is plain text, print it and go back to user input.
```

The shape, intent, and operational character are completely different. That's the point.

## Files in this directory

- `README.md` (this file)
- `financial_data_explorer.py` — illustrative sketch of the SDK pattern; **not currently runnable** (no main entry, no executable LLM call). Lives here to give the comparison concrete code, not to ship a second demo.

## Future work (if a prospect engagement demands it)

If a real engagement wants the SDK version live:

1. Lift the tool definitions out of `src/tools/api.py` into a proper `tool_use`-shaped registry.
2. Add a small CLI in this directory that runs the conversation loop.
3. Add a FastAPI WebSocket route mirroring the loop for the web client.
4. Document it as a separate capability in the Prudentia AI page (`/ai/`) under a new section or alongside the Agent Loops capability.

Until then, the existing Earnings Reaction Playbook is the canonical Agent Loops demo. This directory is reference material.
