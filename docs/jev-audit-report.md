# Comprehensive Jev audit — every symbol, every semantic dimension

*1172 symbols, each asked 10 declared semantic dimensions plus the context-protocol questions, through `driver_core.jev_context.ask_until_grounded`.*

*Root `C:\Users\SCM\Documents\GitHub\driver-core`, model `jev-1.13.0`, gate `0.9`.*

## What the run cost

| metric | value |
|---|---|
| symbols audited | 1172 |
| rounds | 3030 |
| input tokens | 9,075,791 |
| spend | $0.381183 |
| symbols whose loop ended ungrounded | 1172 |

## Verdicts by dimension

| dimension | answered | verdicts | mean confidence | declared buckets |
|---|---|---|---|---|
| `contract_fidelity` | 1172 | `false` 91, `true` 1081 | 0.707 | contract, code |
| `behaviour_defect` | 1172 | `false` 1148, `true` 24 | 0.7901 | code, counterexample |
| `fails_closed` | 1172 | `false` 630, `true` 542 | 0.7195 | code, constraints |
| `reachability` | 1172 | `reachable` 354, `test_only` 719, `unreachable` 99 | 0.9305 | facts |
| `regression_guarded` | 1172 | `false` 694, `true` 478 | 0.7029 | tests, code |
| `disclosure_risk` | 1172 | `false` 1032, `true` 140 | 0.7432 | code, constraints |
| `duplicated_fact` | 1172 | `false` 1169, `true` 3 | 0.8318 | code, alternatives |
| `simpler_shape` | 1172 | `false` 1145, `true` 27 | 0.7183 | alternatives, code |
| `side_effect_class` | 1172 | `filesystem` 157, `local_state` 343, `machine_input` 70, `network` 97, `pure` 505 | 0.6663 | code |
| `docstring_accurate` | 1172 | `false` 565, `true` 607 | 0.6418 | contract, code |

## Which kinds of context these questions needed

The model was asked which declared kind of context would most improve its answer, and which dimension that was for. Each row is an observed pairing, not a theory.

| dimension that would most improve | context kind it needs | times |
|---|---|---|
| `contract_fidelity` | `contract` | 316 |
| `contract_fidelity` | `code` | 251 |
| `reachability` | `code` | 104 |
| `regression_guarded` | `code` | 83 |
| `reachability` | `none` | 69 |
| `docstring_accurate` | `code` | 35 |
| `reachability` | `contract` | 33 |
| `regression_guarded` | `counterexample` | 26 |
| `regression_guarded` | `none` | 22 |
| `regression_guarded` | `tests` | 22 |
| `reachability` | `tests` | 20 |
| `contract_fidelity` | `none` | 18 |
| `reachability` | `receipts` | 15 |
| `duplicated_fact` | `none` | 15 |
| `reachability` | `facts` | 11 |
| `disclosure_risk` | `none` | 10 |
| `docstring_accurate` | `contract` | 9 |
| `behaviour_defect` | `none` | 9 |
| `duplicated_fact` | `contract` | 8 |
| `reachability` | `counterexample` | 7 |
| `behaviour_defect` | `code` | 7 |
| `contract_fidelity` | `facts` | 7 |
| `docstring_accurate` | `none` | 7 |
| `regression_guarded` | `contract` | 6 |
| `duplicated_fact` | `code` | 6 |
| `contract_fidelity` | `tests` | 6 |
| `regression_guarded` | `receipts` | 6 |
| `disclosure_risk` | `code` | 5 |
| `contract_fidelity` | `receipts` | 5 |
| `docstring_accurate` | `receipts` | 4 |
| `disclosure_risk` | `contract` | 4 |
| `regression_guarded` | `facts` | 3 |
| `behaviour_defect` | `counterexample` | 3 |
| `reachability` | `constraints` | 2 |
| `contract_fidelity` | `counterexample` | 2 |
| `behaviour_defect` | `tests` | 2 |
| `disclosure_risk` | `receipts` | 2 |
| `duplicated_fact` | `facts` | 2 |
| `behaviour_defect` | `facts` | 1 |
| `disclosure_risk` | `constraints` | 1 |
| `disclosure_risk` | `counterexample` | 1 |
| `docstring_accurate` | `facts` | 1 |
| `docstring_accurate` | `tests` | 1 |
| `contract_fidelity` | `constraints` | 1 |
| `behaviour_defect` | `receipts` | 1 |
| `side_effect_class` | `code` | 1 |
| `fails_closed` | `code` | 1 |
| `behaviour_defect` | `contract` | 1 |

Buckets asked for across all symbols:

- `code` × 493
- `contract` × 377
- `none` × 150
- `tests` × 51
- `counterexample` × 39
- `receipts` × 33
- `facts` × 25
- `constraints` × 4

## Outstanding need, as the model scored it

A ``score`` question answers on a continuous scale -- the declared
levels are anchors, not the only values returned -- so the scores are
banded. Bands above 0 are the symbols where the model asked for
context and then said it needed no more, which is the honest
"not confident enough to settle" signal.

| context still needed | symbols |
|---|---|
| 1–2 | 59 |
| 2–3 | 522 |
| 3–4 | 456 |
| above 4 | 135 |

## Where nothing could answer

These buckets were named by the model and have no supplier at all:
supplying them would mean *running* the code rather than reading it.
A verdict resting on one of these is reported ungrounded instead of
being reported confidently, and the material is real -- it exists, and
this audit did not obtain it. A bucket the supplier *does* have, asked
for a second time with no new content behind it, is not counted here;
it appears as `no_new_evidence` below, which asks for the opposite
response.

| bucket | times named |
|---|---|
| `counterexample` | 39 |
| `receipts` | 33 |

## Where the loop ended unsettled, and why

Every symbol below ran out of loop without a settled answer, and the
kind says which condition it was. They are listed apart because they
are not the same thing, and a bare count of "ungrounded" would
average them into one number that answers nothing:

* `unanswerable` -- the model named a kind of context no supplier
  has; the material is real and this run did not obtain it.
* `no_new_evidence` -- it asked again for a bucket already holding
  exactly the content supplied. The material is not missing; the
  evidence this supplier has is exhausted.
* `still_short` -- every bucket it asked for was supplied, it then
  said it needed nothing further, and a declared answer is still
  below the gate. The question has no confident answer from this
  evidence, which is a result and not a failure of the run.
* `rounds_exhausted` -- it was still asking for new context when the
  round budget ran out. Nothing is missing; there were too few
  rounds to settle it inside this run's budget.
* `transport` -- the call itself never completed.

| how the loop ended | symbols |
|---|---|
| `no_new_evidence` | 856 |
| `still_short` | 150 |
| `rounds_exhausted` | 94 |
| `unanswerable` | 72 |

Most common reasons:

- 493× the model asked again for 'code', but that bucket already holds exactly this content, so no new evidence is av
- 322× the model asked again for 'contract', but that bucket already holds exactly this content, so no new evidence i
- 135× the model asked for no further context, yet ['behaviour_defect', 'contract_fidelity', 'disclosure_risk', 'docs
- 94× the model was still asking for context when the round budget of 3 ran out, so its answers below the 0.9 gate s
- 39× the model asked for 'counterexample', which no supplier can provide
- 34× the model asked again for 'tests', but that bucket already holds exactly this content, so no new evidence is a

## Verdicts a mechanism can contradict

Symbols the model called contract-unfaithful, unguarded, or wrongly
documented, printed beside the number of test files that actually
name them. A verdict of "unguarded" against a symbol several tests
name is one a reader can see is wrong -- which is why both numbers
sit in one table.

Total such verdicts: **1350**.

| symbol | dimension | test files naming it | lines |
|---|---|---|---|
| `driver_core.actions:Action` | `docstring_accurate` | 2 | 84 |
| `driver_core.actions:Action.__init__` | `docstring_accurate` | 5 | 28 |
| `driver_core.actions:Action.severity` | `regression_guarded` | 0 | 2 |
| `driver_core.actions:Action.requires_consent` | `regression_guarded` | 0 | 3 |
| `driver_core.actions:Action.check_params` | `docstring_accurate` | 1 | 21 |
| `driver_core.actions:Action.__repr__` | `regression_guarded` | 0 | 2 |
| `driver_core.actions:_declare` | `regression_guarded` | 0 | 57 |
| `driver_core.actions:Vocabulary.__init__` | `regression_guarded` | 5 | 10 |
| `driver_core.actions:Vocabulary.__init__` | `docstring_accurate` | 5 | 10 |
| `driver_core.actions:Vocabulary.of_class` | `regression_guarded` | 0 | 3 |
| `driver_core.actions:Vocabulary.identity` | `regression_guarded` | 3 | 2 |
| `driver_core.actions:Vocabulary.__len__` | `regression_guarded` | 0 | 2 |
| `driver_core.actions:Vocabulary.__repr__` | `regression_guarded` | 0 | 2 |
| `driver_core.actions:check_batch` | `contract_fidelity` | 1 | 18 |
| `driver_core.actions:check_batch` | `docstring_accurate` | 1 | 18 |
| `driver_core.adapters:_launch_failure` | `regression_guarded` | 0 | 26 |
| `driver_core.adapters:_path_like` | `regression_guarded` | 0 | 12 |
| `driver_core.adapters:ScreenSource.__init__` | `regression_guarded` | 5 | 9 |
| `driver_core.adapters:ScreenSource.can_serve` | `regression_guarded` | 1 | 6 |
| `driver_core.adapters:ScreenSource.can_serve` | `docstring_accurate` | 1 | 6 |
| `driver_core.adapters:ScreenSource.capture` | `regression_guarded` | 10 | 28 |
| `driver_core.adapters:ScreenSource.capture` | `docstring_accurate` | 10 | 28 |
| `driver_core.adapters:CliSource.__init__` | `regression_guarded` | 5 | 12 |
| `driver_core.adapters:CliSource.__init__` | `docstring_accurate` | 5 | 12 |
| `driver_core.adapters:CliSource.can_serve` | `regression_guarded` | 1 | 4 |
| `driver_core.adapters:CliSource.can_serve` | `docstring_accurate` | 1 | 4 |
| `driver_core.adapters:CliSource.capture` | `regression_guarded` | 10 | 23 |
| `driver_core.adapters:CliSource.capture` | `docstring_accurate` | 10 | 23 |
| `driver_core.adapters:McpSource` | `contract_fidelity` | 1 | 81 |
| `driver_core.adapters:McpSource.__init__` | `regression_guarded` | 5 | 13 |
| `driver_core.adapters:McpSource.__init__` | `docstring_accurate` | 5 | 13 |
| `driver_core.adapters:McpSource.can_serve` | `regression_guarded` | 1 | 4 |
| `driver_core.adapters:McpSource.can_serve` | `docstring_accurate` | 1 | 4 |
| `driver_core.adapters:McpSource._envelope` | `contract_fidelity` | 0 | 9 |
| `driver_core.adapters:McpSource._envelope` | `regression_guarded` | 0 | 9 |
| `driver_core.adapters:McpSource.capture` | `regression_guarded` | 10 | 34 |
| `driver_core.adapters:McpSource.capture` | `docstring_accurate` | 10 | 34 |
| `driver_core.adapters:DomSource` | `regression_guarded` | 0 | 48 |
| `driver_core.adapters:DomSource.__init__` | `regression_guarded` | 5 | 5 |
| `driver_core.adapters:DomSource.can_serve` | `docstring_accurate` | 1 | 4 |
| `driver_core.adapters:DomSource.capture` | `regression_guarded` | 10 | 19 |
| `driver_core.adapters:DomSource.capture` | `docstring_accurate` | 10 | 19 |
| `driver_core.adapters:_default_encoder` | `regression_guarded` | 0 | 3 |
| `driver_core.adapters:_default_encoder` | `docstring_accurate` | 0 | 3 |
| `driver_core.audit:_now` | `regression_guarded` | 1 | 2 |
| `driver_core.audit:same_chain` | `regression_guarded` | 0 | 16 |
| `driver_core.audit:AuditLog.__init__` | `regression_guarded` | 5 | 7 |
| `driver_core.audit:AuditLog.__init__` | `docstring_accurate` | 5 | 7 |
| `driver_core.audit:AuditLog._tail` | `regression_guarded` | 0 | 26 |
| `driver_core.audit:AuditLog.count` | `regression_guarded` | 3 | 3 |
| `driver_core.audit:AuditLog.head` | `regression_guarded` | 3 | 3 |
| `driver_core.audit:AuditLog.head` | `docstring_accurate` | 3 | 3 |
| `driver_core.audit:AuditLog.append` | `docstring_accurate` | 10 | 18 |
| `driver_core.audit:AuditLog._write` | `regression_guarded` | 0 | 10 |
| `driver_core.audit:AuditLog._write` | `docstring_accurate` | 0 | 10 |
| `driver_core.audit:ChainVerdict` | `regression_guarded` | 0 | 16 |
| `driver_core.audit:ChainVerdict.__init__` | `regression_guarded` | 5 | 4 |
| `driver_core.audit:ChainVerdict.__bool__` | `regression_guarded` | 0 | 2 |
| `driver_core.audit:ChainVerdict.__repr__` | `regression_guarded` | 0 | 2 |
| `driver_core.audit:MemoryAuditLog.__init__` | `regression_guarded` | 5 | 3 |

