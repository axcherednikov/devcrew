# Original-intent acceptance evaluation

This evaluation separates deterministic structural evidence from semantic and
Desktop acceptance. Passing fixtures can verify identities, report shape,
state transitions and stale-input rejection; they cannot prove that a model
understood an original request. The real semantic probes and installed
Desktop scenarios below are procedures for the coordinator. They are **NOT
RUN** by this document or by the bounded implementation checks.

## Required scenarios and deterministic coverage

| Scenario | Required behavior | Existing structural coverage |
|---|---|---|
| R1 — Prompt capture | A supported `UserPromptSubmit` event stores a SHA-256 digest of the exact prompt bytes and session, turn, and project provenance; replay is idempotent and conflicting replay cannot replace it. | `tests/test_original_intent.py`: `test_exact_capture_identity_and_claim`, `test_identical_replay_is_idempotent_but_conflicting_replay_is_rejected`, `test_capture_is_private_bounded_and_rejects_oversize_prompt`, `test_main_emits_capture_metadata_and_preserves_learning_context`. |
| R2 — Source identity | Exact attachments and documents are snapshotted with immutable identity; unavailable sources stay explicitly unverified; a supplied attachment is not relabeled as a captured prompt. | `tests/test_original_intent.py`: `test_file_source_is_snapshotted_and_replay_is_stable_after_input_changes`, `test_unavailable_source_is_explicit_and_has_stable_identity`, `test_full_prompt_capture_binding_is_forbidden`, `test_source_bind_cli_runs_before_manifest_init_and_returns_metadata_only`. |
| R3 — Minimal v2 intent | Stable requirement IDs, source fragments, obligation/fact/technical-decision kind, phase/acceptance references, addressed decisions, and existing evidence references remain bound to the current plan digest without duplicating acceptance/evidence. | `tests/test_task_phases.py`: `test_v2_requirement_binds_exact_source_bytes_and_acceptance_reference`; `tests/test_original_intent.py`: `test_effective_requirements_and_digest_projection`, `test_addressed_overlay_validates_and_original_definition_is_retained`. |
| R4 — Planning omission | The coordinator's source-read assessment must expose an omitted source obligation before plan acceptance. A complete derived matrix is not semantic proof, and no mandatory planning-reviewer dispatch is added. | Structural identity/coverage checks: `tests/test_original_intent.py`: `test_missing_source_coverage_stale_digest_and_unauthorized_protected_writes_fail`. The omitted-card judgment requires the separate semantic probe below; no deterministic matrix test is counted as semantic proof. |
| R5 — Business changes | Unapproved removal or weakening, including changed acceptance mapping, fails or remains pending. A verified decision naming affected requirement IDs authorizes only that change and preserves the original definition/history. Technical corrections remain autonomous. | `tests/test_phase_review_lifecycle.py`: `test_v2_systemic_replan_rejects_incomplete_explicit_obligation_assessment`, `test_v2_systemic_replan_accepts_complete_technical_acceptance_remapping`, `test_v2_last_obligation_removal_survives_systemic_recovery_and_later_technical_replan`; `tests/test_original_intent.py`: `test_addressed_overlay_validates_and_original_definition_is_retained`. |
| R6 — Final independent review | Accepted phases alone do not finish a task. A fresh routed reviewer binds an independent intent report to the exact candidate/intent/evidence and detects an unmapped omission despite a complete derived matrix. | `tests/test_phase_review_lifecycle.py`: `test_final_v2_pass_requires_fresh_complete_intent_review_and_accepts_task`, `test_final_v2_pass_rejects_missing_intent_review`, `test_final_v2_pass_rejects_stale_intent_review_binding`, `test_final_v2_pass_rejects_unmapped_source_omission_despite_complete_matrix`, `test_generic_manifest_write_cannot_manufacture_task_acceptance`; `tests/test_reviewer_brief.py`: `test_final_v2_reviewer_contract_carries_canonical_original_intent`. |
| R7 — Staleness | Changed source bytes, stale source/decision identity, altered report/evidence/log, stale candidate, or stale intent-review binding fails at its applicable boundary. | `tests/test_original_intent.py`: `test_file_source_is_snapshotted_and_replay_is_stable_after_input_changes`, `test_stale_cas_precedes_callbacks`; `tests/test_phase_review_lifecycle.py`: `test_final_v2_pass_rejects_stale_intent_review_binding`, `test_acceptance_rejects_missing_or_edited_host_report`, `test_resume_later_phase_validates_archived_cycles_of_accepted_predecessors`; `tests/test_phase_concurrency.py`: `test_assessed_v2_competing_status_writers_preserve_original_intent`. |
| R8 — Diagnosis and DIRECT | Short diagnosis-first work can discover facts and later test criteria without inventing business choices; DIRECT keeps its existing lightweight path. | `tests/test_task_phases.py`: `test_v2_original_intent_allows_empty_diagnostic_requirements_only_when_pending`; `tests/test_task_execution_policy.py`: `test_direct_is_all_conditions_and_does_not_require_agents`; `tests/test_original_intent.py`: `test_unavailable_source_is_explicit_and_has_stable_identity`. |
| R9 — Resume and history | Resume, reapproval/replan, accepted cycles, decisions, source identities, assessment history and compare-and-swap preserve bindings; stale or concurrent writes cannot erase them. | `tests/test_pre_candidate_reapproval.py`: `test_v2_pre_candidate_reapproval_archives_intent_and_assessment_history`, `test_cli_v2_pre_candidate_reapproval_accepts_separate_assessment`, `test_cli_v2_replan_complete_accepts_separate_assessment`; `tests/test_phase_concurrency.py`: `test_assessed_v2_competing_status_writers_preserve_original_intent`; `tests/test_phase_review_lifecycle.py`: `test_v2_systemic_recovery_rechecks_sources_inside_locked_callback`. |
| R10 — Compatibility and lifecycle | Schema-v1 remains readable without automatic migration/certification; existing verification, candidate, independent review and waiting lifecycle remain intact. | `tests/test_task_phases.py`: v1 `test_manifest_resume_loads_without_planner`, `test_v2_original_intent_allows_empty_diagnostic_requirements_only_when_pending` (v1 rejects v2-only intent fields); `tests/test_pre_candidate_reapproval.py`: `test_v1_initialization_rejects_intent_assessment_input`; `tests/test_phase_review_lifecycle.py`: existing candidate/review/acceptance tests plus `test_final_v2_pass_requires_fresh_complete_intent_review_and_accepts_task`. The structural compatibility tests do not certify v1 completion; full required lifecycle and package verification remains part of final integrated acceptance. |

## Real separate-context semantic probes — NOT RUN

Run each probe in a real, separate Codex context with no inherited reasoning
history (`fork_turns=none`) after the tested plugin snapshot is available. Use
supported source binding and preserve the actual host outputs. For each probe,
record the literal prompt below, source snapshot IDs and SHA-256 values, the
source reads shown in the host assessment, the final plan/acceptance mapping,
reviewer outcome, observed command/report status, and any limitations. Do not
replace the run with a boolean, matrix fixture, or generated transcript.

**Probe A — omitted bank-card obligation.** Submit exactly:

> When SBP is unavailable, recommend a bank card. Keep the card option available in that case. Inspect the current project and propose a plan for this behavior.

Bind the provided text as an explicit source file, or use the user-confirmed Intent Contract procedure. Ask the coordinator to
read the source and assess its obligations. In a separate planning context,
observe whether the plan carries both recommending a card when SBP is blocked
and keeping the card option available. The coordinator assessment must flag a
missing obligation before accepting a plan that omits either one. Then use a
fresh routed final reviewer to independently read the source and inspect the
final candidate/evidence; verify it reports any unmapped missing outcome even
if the derived matrix is otherwise complete. Record the actual report and
whether the existing lifecycle remains incomplete on FAIL.

**Probe B — short diagnosis before criteria.** Submit exactly:

> Why does the card recommendation sometimes disappear when SBP is unavailable? First investigate the existing flow and report what you can establish. Do not choose a business rule for me. After diagnosis, suggest testable criteria based on the findings.

Record which facts were established, which questions remain open, and whether
the coordinator waits for an owner choice only for an actual business boundary.
The initial request should not receive invented product obligations or a
mandatory complete upfront specification. If criteria are proposed, bind them
to the observed facts and preserve the unresolved choice for the owner.

## Installed Desktop scenarios — NOT RUN

Only after the coordinator installs the tested snapshot through supported
plugin operations, compare the installed hook, scripts, skill files and role
instructions with the tested source snapshot. Do not edit plugin caches, user
hooks or global settings manually. Use the normal hook trust flow and record
whether permission/trust was granted.

- **Desktop A:** submit the omitted-bank-card prompt above in the target
  project. Record the source identity and provenance of the file or confirmed contract, bound
  snapshot identity, coordinator source reads, planned requirement/acceptance
  mapping, and final independent review result.
- **Desktop B:** submit the short diagnosis prompt above. Record actual facts,
  criteria, unresolved choices, and whether the response avoided invented
  business rules or a full upfront specification.

Record screenshots or UI artifacts only through the ordinary supported
Desktop flow. If plugin permission, normal hook trust, or UI access is
unavailable, mark that scenario **NOT VERIFIED**; do not report Desktop
acceptance as PASS. Structural tests do not stand in for these observations.
