# Test Catalogue — chaos (S), property (P), contract (C)

Normative vehicle index for component-architecture §18 invariants and
Protocols §29 criterion 12. Status: **done** (pytest, file cited),
**partial** (unit-covered, procedure/drill missing), **planned**.

## S — chaos scenarios

| ID | Scenario | Status | Evidence / owner |
|---|---|---|---|
| S01 | Normal arbitrage migration, SUCCESS + epoch+1 | done | `tests/test_e2e_emulated.py` |
| S02 | Short-job arbitrage rejected | done | `tests/test_policy_v2.py` |
| S03 | Emergency parallel checkpoint ∥ provision, single provision | done | `tests/test_execution_kernel.py` |
| S04 | Deadline exhausted pre-fence → abort/replan path | done (emulated) | `deadline-drift`/`plan-expired` signals + bounded successor execution (`orchestrator/replan.py`, `tests/test_replan_wiring.py`); live deadline drill scheduled |
| S05 | Corruption during persist → never DURABLE | done | `tests/test_transition_stores.py` (checksum + manifest gates) |
| S06 | Fencing failure → `RECONCILIATION_REQUIRED`, no inferred success | done | `tests/test_execution_kernel.py` |
| S07 | Split-brain / ownership conflict → gate, no auto-choice | partial | gate unit done (`tests/test_correctness.py`); live drill planned (Track C4) |
| S08 | UNKNOWN provisioning outcome → resolve, never blind retry | done (emulated) | probe resolution without blind retry (`tests/test_outcome_resolution.py`); live provider-query path scheduled |
| S09 | CPR crash at every migration state → resume, RPO 0 | done (emulated) | 10-state frontier matrix + UNKNOWN variants (`tests/test_bootstrap_drills.py`); startup wiring in `main.py`; live crash matrix + RPO proof scheduled (Track B4) |
| S10 | CPR crash mid-FENCING → matrix outcome | done (emulated) | fence-adjacent unknowns gate on reconciliation, never inferred; live fencing-matrix procedure scheduled (Track B4) |
| S11 | Fault-inject after FENCING-start → no rollback action | done (emulated) | no-abort transition + action-level test (no cleanup, no target terminate, single fence sequence; `tests/test_execution_kernel.py::test_no_rollback_action_after_fencing_start`) |
| S12 | Orphan/stale/dangling sweep findings | done | `tests/test_correctness.py` |
| S13 | Fencing-matrix rows 4–6 refuse to auto-resolve | partial | gate units done; matrix procedure planned |
| S14 | Duplicate/out-of-order events → dedup, no double-actuation | done (emulated) | ledger dedup + FAILED-retry-one-attempt + cross-type interleave-once (`tests/test_interruption_ingestion.py`); dispatcher is synchronous by design — a queued backlog with ordering guarantees remains deferred |
| S15 | REPLAN → successor plan via `supersedes_plan_id` | done (emulated) | signal → budget → fresh-estimate successor → SUCCESS, epoch+1 once (`tests/test_replan_wiring.py::test_s15_expired_plan_replans_to_success`); durable chain-walk budget deferred to Plan Store wiring |
| S16 | Emergency supersedes pre-fence arbitrage | partial | pre-emption mechanism built (`replan.preempt_with_emergency` + live `supersede_and_replan` with regime override, coordinator-tested); plans admitted to Plan Store with terminal-outcome marking + `find_preemptible_plan` lookup (`tests/test_main_wiring.py`); trigger firing + typed-plan rehydration + step-resume execution remain Track B |
| S17 | Clock expires mid-FENCING → safe conclusion | done (emulated) | expired post-fence frontiers resume forward-only (COMPLETE_FENCING/I10), pre-fence expire abandons; live clock drill scheduled |
| S18 | Post-fence INVALID → forward-only, epoch stands | done | `tests/test_execution_kernel.py` |
| S19 | INCONCLUSIVE validation → Recovery Policy, never silent promote | done (emulated) | validator verdicts + stale-L2 → INCONCLUSIVE + real-validator coordinator paths + sinkless INCONCLUSIVE never-promotes (`tests/test_validator_probes.py`, 12 tests); live executor drills scheduled |
| S20 | GC with locked/active checkpoints → never deletes | done | `tests/test_transition_stores.py` |
| S21 | Duplicate interruption event → single recovery | done (emulated) | stable `event_id` dedup + in-process seen-set + ledger gate (`orchestrator/interruption_ingestion.py`, `tests/test_interruption_ingestion.py`); live IMDS redelivery drill scheduled |
| S22 | Provider throttle/outage → backoff, deadline semantics | done (emulated) | throttle-class retry/backoff + deadline-yield harness (`tests/test_outcome_resolution.py`); live provider drill scheduled |
| S23 | UNKNOWN at every executor | done (emulated) | checkpoint/transfer/restore/validate/provision UNKNOWN matrix (`tests/test_outcome_resolution.py`); live executor drills scheduled |
| S24 | KMS denied → candidate NOT_READY, abort pre-irreversible | planned | readiness flag exists; decrypt-proof unbuilt |
| S25 | Home-region outage → halt, sweep-after-restore | planned | accepted limitation; drill scheduled (Track E2) |

## P — invariant properties (component-architecture §18 IA-1, I1–I15; note Protocols I14 is a different invariant — "validation ≠ ownership")

| Invariant | Vehicle | Status |
|---|---|---|
| IA-1 crash ≠ workload failure | S09 drill | planned |
| I1 one active migration | `test_transition_stores.py` (registry guard) | done |
| I2 fencing irreversible | no-abort transition + epoch rotation tests | done |
| I3 source protected pre-confirm | fencing fail-closed test | done |
| I4 no blind UNKNOWN retry | timeout→UNKNOWN + resolution-matrix tests | done (emulated) |
| I5 one mutation path | transition CAS tests (both backends) | done |
| I6 plan immutability | hash + duplicate-put + step-CAS tests | done |
| I7 operation idempotency | provision/transfer/persist triple-execute tests | done (emulated) |
| I8 worker authority prohibition | controller primitive-only + refusal tests | done (emulated) |
| I9 reconciliation gate | gate + refusal-to-decide tests | done |
| I10 deadline vs fencing | code path exists | planned (S17) |
| I11 one authority per datum | store ownership tests | done |
| I12 engine immutability | `test_models_v2.py` pinning | done (router default v2 + frozen-V1 warning/surface pins; deletion diff reviewable) |
| I13 partial ≠ durable | S05 | done |
| I14 audit-before-irreversible | `tests/test_audit_gate.py` (fail-closed fence gate, terminal audit) | done (emulated) |
| I15 UNKNOWN ineligible | placement hard-filter tests | done |

## C — contract suites

| ID | Contract | Suite |
|---|---|---|
| C-REG | Registry CAS + epoch rotation + guards | `tests/test_transition_stores.py` |
| C-LEDGER | put-if-absent dedup per consumer | `tests/test_transition_stores.py` |
| C-PLAN | hash stability, TTL, DAG deps, step CAS | `tests/test_models_v2.py`, `tests/test_execution_kernel.py` |
| C-ID | ULID uniqueness, sole minter | `tests/test_execution_kernel.py` |
| C-CKPT | durability monotonicity, locks, GC | `tests/test_transition_stores.py` |
| C-VALID | L1–L4, verdicts, epoch/lineage, tolerances | `tests/test_correctness.py` |
| C-ECON | hysteresis, short-job, overhead ceiling, net-benefit | `tests/test_policy_v2.py` |
| C-PLACE | v3 weights, normalization, tie-break, expiry | `tests/test_placement_v3.py` |
| C-RECON | taxonomy, lifecycle, gate, remediation paths | `tests/test_correctness.py` |
| C-WORKER | progress durability, resume-exact, handler mapping | `tests/test_worker.py` |
| C-E2E | emulated migration + overhead gate | `tests/test_e2e_emulated.py`, `tests/test_overhead.py` |
| C-TF | TF static guards | `tests/test_infra_tf.py` |

Every orchestration/persistence PR must cite the S/P/C rows it exercises.
