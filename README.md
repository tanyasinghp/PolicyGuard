# Design decisions

A running log of consequential choices, each with its rationale and — where
one exists — the incident that motivated or validated it. Several were
surfaced by tests or by fail-closed enforcement during development; those
incidents are recorded deliberately, as evidence about the approach itself.

1. **Three-valued verdicts (ALLOW / BLOCK / ESCALATE).** Approval-required
   actions are a distinct human-oversight category, not a soft block.
   ESCALATE is load-bearing in the PEP and data-export flows.
2. **Derivations are structured objects, not prose.** Audit
   reconstructability can only be *measured* if derivations are
   first-class data (`Derivation`, `ResolutionStep`); rendering to text is
   a view, not the record.
3. **Rule conditions are scalars only (`str | bool | int`).** This keeps
   the checker's applicability matching decidable. Everything richer is
   deliberately pushed into the mapper — which is exactly where the
   project's "semantic gap" measurement lives.
4. **`Rule` is frozen.** Rules are immutable facts once compiled:
   hashable, safe to share, immune to in-flight mutation bugs.
5. **Fail-closed by default, as a constructor parameter.** Actions no rule
   governs are blocked unless configured otherwise; the fail mode is a
   measured experimental knob, not a hidden assumption. *Incidents:*
   fail-closed mechanically exposed two policy incompletenesses during
   development — missing permissions for process steps (verify/screen/
   risk-rate/EDD) and for standard-CDD account opening. Fail-closed
   enforcement forces policy completeness, the same property
   capability-based systems (cf. CaMeL) have.
6. **Obligations gate; permissions license.** On ALLOW, the governing rule
   is the licensing permission, not a satisfied obligation. *Incident:*
   found by a failing unit test that expected `SDD-LOWRISK` and got
   `IDENTIFY-FIRST`; fixed in `Checker._verdict`, encoding a deontic
   principle directly in code.
7. **Tools enforce nothing.** The world must permit harm or the experiment
   has no control condition. Pinned by
   `agent/tests/test_tools.py::test_tools_enforce_nothing`, which will
   fail if anyone adds a well-meaning check inside a tool.
8. **The guard owns the prior-actions registry.** `check()` reads priors
   only from `record_execution()` calls made after actual execution; there
   is no parameter through which a caller (or an injected transcript
   claim) could assert priors. The injection defense is structural, not
   behavioral. Preserved across the network boundary by
   `/v1/record_execution`.
9. **Mapper failure → ESCALATE, never ALLOW or BLOCK.** Uncertainty routes
   to a human, with the reason recorded in derivation notes; abnormal
   paths are audited too.
10. **The oracle mapper is honest, not omniscient.** It reads only facts
    that executed tools revealed, so oracle-vs-LLM cleanly measures
    mapping quality, not information access. Its `discloses_str` keyword
    heuristic is a documented simplification (and an argument for LLM
    mapping of genuinely semantic conditions).
11. **Identical policy knowledge across conditions.** Both prompts render
    policy text programmatically from the compiled graph; conditions
    differ only in the enforcement paragraph. Measured differences are
    attributable to enforcement, not information.
12. **Ground truth by replay adjudication.** Executed actions are re-ruled
    by the deterministic checker against honest replay state; no metric
    consults the guard's own logs. `guarded_oracle`'s zero violations are
    reported as ceiling-by-construction, never as an empirical victory.
13. **Mapper confidence is recorded, never used.** There is deliberately
    no "the model felt sure, so allow it" path; confidence exists for
    analysis only.
14. **Blocked ≠ crashed.** BLOCK/ESCALATE return the derivation to the
    agent as an error tool result so it can satisfy unmet obligations and
    continue — otherwise over-refusal and completion metrics would
    conflate enforcement strictness with unrecoverable dead ends.
15. **`Any` at SDK boundaries; `ANN401` configured off.** The Anthropic
    client's response types are not ours to pin; typed Protocols cover the
    slice we use, and tests inject stubs. Fake precision was rejected in
    favor of an explicit lint configuration.