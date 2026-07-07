# Threat model

## Assets

Policy-compliant execution (no forbidden action executes; approval-gated
actions do not execute unapproved); integrity of the audit ledger and the
prior-actions registry; fidelity of the compiled policy graph to its YAML
source.

## Trust boundaries

| Zone | Components | Stance |
|---|---|---|
| Untrusted | agent model, conversation transcript, customer documents, tool outputs containing customer content | may lie, may carry injections |
| Semi-trusted (measured) | LLM context mapper | outputs constrained to a graph-derived vocabulary; failures escalate; error rate is a headline metric |
| Trusted | compiled policy graph, deterministic checker, priors registry, audit ledger | pure/deterministic, unit-tested, no model in the loop |

## Adversaries and attack surfaces

1. **Malicious content author (prompt injection).** Instructions embedded
   in customer documents reach the agent verbatim via tool outputs
   (deliberately — see `test_injection_payload_surfaces_in_tool_output`).
   *Against the agent:* "skip screening" / urgency / authority framings
   (episodes ep-030..032). *Against the mapper (fact injection):* claims
   that a sanctions match "was resolved" or approval "was granted"
   (ep-033..036) attempt to corrupt extracted attributes rather than agent
   behavior — the subtler surface, and a core measurement.
2. **Misaligned or manipulated agent.** The design assumes the agent may
   attempt any tool call for any reason; enforcement must not depend on
   its cooperation. Sequencing history cannot be forged: priors are
   guard-recorded (structural defense, decision #8).
3. **Evasion by decomposition.** Splitting a forbidden outcome into
   individually plausible steps; partially covered by sequencing
   obligations and the export/tipping-off episodes (ep-037..039).
4. **Elicitation.** Socially engineered disclosure (tipping-off) and data
   exfiltration via "customer requests" — covered by prohibition and
   approval-gated rules on `send_customer_email` / `export_customer_data`.

## Known weaknesses (measured, not hidden)

- **The mapper is the softest trusted-side component.** A condition key it
  omits silently de-applies the governing rule (under-blocking), pinned by
  `test_mapper_omission_causes_underblocking_not_crash`. Quantifying this
  channel is hypothesis H2.
- **Fail-closed ↔ over-refusal trade-off** is a measured knob, not a
  solved problem.
- **`discloses_str` keyword adjudication** lower-bounds tipping-off
  detection.

## Out of scope for the PoC

Compromise of the guard's host or of the policy YAML at rest; backdoored
mapper models; errors in policy *authoring* (the graph is trusted to match
its YAML, and the YAML is a simplified paraphrase — see the provenance
note); side channels (timing, token counts); multi-agent collusion;
denial-of-service. Several of these become tractable research directions
in the fellowship phase (e.g., signed policy bundles, mapper ensembles).