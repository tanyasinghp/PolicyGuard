# Policy graph schema

The formal semantics of `rules_sg_mas626.yaml`, as implemented by
`policy/compile_graph.py` (structure) and `guard/checker.py` (decision
procedure). This document is normative for rule authors: if a rule can't
be expressed within these semantics, that is a finding about the
formalism's expressiveness boundary (hypothesis H2), not something to
work around with ad-hoc code.

## Node types

| Node | Meaning |
|---|---|
| `Rule` | one normative statement with deontic force |
| `ActionType` | a canonical agent action (1:1 with tool names in this PoC) |

Rules carry: `deontic` status, applicability `conditions`, sequencing
requirements (`requires_prior`), an approval requirement
(`requires_approval`), defeat edges (`overrides`, `excepts_from`), a
`priority` band, temporal validity (`valid_from` / `valid_to`,
inclusive), a `jurisdiction`, provenance (`derived_from`), and a
one-line `rationale` used verbatim in derivations.

## Deontic statuses

- **obligation** — the action is required to satisfy the rule; in this
  PoC obligations act through `requires_prior` (sequencing: "X must
  precede Y") and never themselves license an action.
- **permission** — licenses the action when the rule applies and
  survives resolution. On ALLOW, the governing rule is always a
  permission (obligations gate; permissions license — design decision #6).
- **prohibition** — forbids the action; any surviving applicable
  prohibition yields BLOCK regardless of surviving permissions.

## Applicability

A rule applies to a proposed action iff **all** hold:

1. `rule.action == context.action_type`;
2. the rule is in force on `context.timestamp` (validity interval,
   inclusive both ends);
3. `rule.jurisdiction == context.jurisdiction`;
4. every key in `rule.conditions` is present in `context.attributes`
   with an exactly equal scalar value.

Condition values are scalars only (`str | bool | int`) — the
decidability boundary. A condition key **absent** from the context is a
non-match: the rule does not apply. Consequence: mapper omissions
surface as potential under-blocking, which the evaluation measures
(H2); they never crash the checker.

## Defeat (conflict resolution)

Applied pairwise over the applicable set, in strength order:

1. **`excepts_from`** — explicit exception edge; reported as
   ground `"exception"`. Semantically: this rule carves a hole in the
   target's scope.
2. **`overrides`** — explicit override edge; ground
   `"explicit-override"`.
3. **priority** — numeric comparison, used **only** between genuinely
   conflicting rules (a prohibition vs. a permission on the same
   action); ground `"priority"`. Obligations never conflict — they add
   requirements.
4. **conservative tie-break** — equal-priority prohibition/permission
   conflicts resolve for the prohibition; ground
   `"conservative-tie-break"`.

Explicit edges always beat priority. The defeat relation must be
acyclic; cycles are a compile-time error, as are dangling edge targets
and duplicate rule IDs.

## Verdict ordering (over surviving rules)

1. any prohibition → **BLOCK** (governing: highest-priority prohibition)
2. any unmet `requires_prior` → **BLOCK** (obligation unmet, named)
3. any `requires_approval` → **ESCALATE** (governing: that rule)
4. any permission → **ALLOW** (governing: highest-priority permission)
5. nothing governs → the configured **fail mode** (default BLOCK,
   i.e. fail-closed: policy must contemplate every permitted action)

## Priority bands (convention, not mechanism)

| Band | Use |
|---|---|
| 100 | absolute prohibitions (sanctions, anonymity) |
| 80–90 | strong prohibitions (tipping-off, export-during-STR) |
| 50–60 | risk-based obligations, exceptions, approval rules |
| 40 | sequencing obligations |
| 5–30 | permissions (process steps, SDD, standard CDD, defaults) |

Bands leave headroom for insertion without renumbering. Explicit defeat
edges are preferred over priority whenever the relationship between two
specific rules is known — priority is the fallback for unanticipated
overlaps, and every priority-ground resolution in an audit log is worth
reviewing as a possible missing explicit edge.

## Authoring checklist

Before adding a rule: does its condition vocabulary already exist in
`guard/mapper.py::_VALUE_HINTS` (if not, add the hint — the key
vocabulary itself is derived from the graph automatically)? Does it need
an explicit defeat edge against any rule sharing its action? Is its
provenance citation verifiable? Run
`python -m policy.compile_graph policy/rules_sg_mas626.yaml` (compile
validation) and the checker test suite afterwards; fail-closed
enforcement plus the episode suite will expose completeness gaps.