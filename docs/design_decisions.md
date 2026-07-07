# Design Decisions

## Why NetworkX instead of Neo4j?

NetworkX is pure Python, requires no external server, and is sufficient for the modest policy graphs we expect (< 100 rules). The `models.py` types are structured so that a Neo4j adapter can be swapped in later (the `pyproject.toml` lists `neo4j` as an optional dependency).

## Why compile rules into a graph at all?

A graph representation allows us to:
- Attach metadata (severity, category) to each rule node.
- Model rule dependencies (e.g., all denials trigger an audit-log rule).
- Run graph algorithms (reachability, impact analysis) in the future.

A flat list of rules would be simpler but harder to extend.

## Separate mapper from checker?

The mapper normalises LLM-specific argument names into a consistent key space (`action`, `path`, `command`). This keeps policy rules independent of the LLM backend. If the LLM changes how it names arguments, only the mapper needs updating.

## Why both Anthropic and OpenAI?

The research questions ask whether graph-based guarding is effective across different LLM backends. Supporting both allows comparative evaluation.

## Streamlit for the demo?

Streamlit gives an interactive UI with minimal code — ideal for a research prototype. If this were production, we would use a proper frontend framework.

## No database?

The prototype stores everything in memory. Evaluation results are written to JSON. A production system would need persistent storage for audit logs and policy versions.
