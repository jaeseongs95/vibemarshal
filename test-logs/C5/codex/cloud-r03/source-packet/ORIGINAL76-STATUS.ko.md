# 원76 완료·잔여 대조와 다음 공통 5건

원76은 완료5 + 잔여71이다. 다음 후보는 M 모듈에서 같은 private fixture/SQLite를 쓰는 5건을 한 묶음으로 준비했다. 이 5건은 NOT_RUN이며 새 SOURCE/PRE/실행 조건은 아직 발급되지 않았다. 원 assertion/helper/oracle bytes를 보존한다.

## 집계와 상태 의미

| 구분 | 수 | 이번 판정 |
|---|---:|---|
| 완료 | 5 | C3/R021 3건 + U2/R029 2건, helper70f5 Linux3.12.14 no-site 사본 근거 |
| 다음 공통 후보 | 5 | named-call 무 subprocess 4건 + READY 사전 거절 1건, 전부 NOT_RUN |
| Windows 의미 필요 | 9 | 현재 Linux profile에서 원 fixture/cwd oracle 보존 불가 |
| M 효과·분기 검토 | 41 | snapshot/Git 잠재 경로; 정상 도달·사전 거절 구별 미결 |
| 다른 module identity·효과 | 16 | D12/H1/I3 전부 기록. 원 import/namespace 및 subprocess 차이 미결 |

미선정66을 전부 불가능이나 최종제외로 단정하지 않는다. Windows9의 현재 profile 불일치와 다른 미결·효과 계약 요구를 구별한다.

## 실제 완료 5건

- C3: [result](C:\Users\sjs95\Documents\Codex\2026-10-04\task-8\vm-cloud-platform-24cfe63e\lead-post-c3-r03-r02-01\raw\run\result.json) 707B/98b90a2aaab88b697c52ce69b3f38d9027327838142bc3e4aa9cbcb31fb29989; [loader](C:\Users\sjs95\Documents\Codex\2026-10-04\task-8\vm-cloud-platform-24cfe63e\lead-post-c3-r03-r02-01\raw\run\loader.json), [execution](C:\Users\sjs95\Documents\Codex\2026-10-04\task-8\vm-cloud-platform-24cfe63e\lead-post-c3-r03-r02-01\raw\run\execution.json), [판정](D:\claude\독립감사관\R-021-VM-C3-FINAL-R03-POST-VERDICT.ko.md). 원 ID/수량/exit0와 source3 불변을 직접 읽었다.
- U2: [result](C:\Users\sjs95\Documents\Codex\2026-10-04\task-8\vm-cloud-platform-24cfe63e\lead-post-u2-r02-01\raw\run-raw\result.json) 582B/06a539d8782d5c8a011ca4ccb750ea5d5ce96c648bb376b6f6e9db7e2c4c4116; [loader](C:\Users\sjs95\Documents\Codex\2026-10-04\task-8\vm-cloud-platform-24cfe63e\lead-post-u2-r02-01\raw\run-raw\loader.json), [execution](C:\Users\sjs95\Documents\Codex\2026-10-04\task-8\vm-cloud-platform-24cfe63e\lead-post-u2-r02-01\raw\run-raw\execution.json), [판정](D:\claude\독립감사관\R-029-VM-U2-R02-POST-VERDICT.ko.md). 원 ID/수량/exit0와 source3 불변을 직접 읽었다.

정확 result/loader ID를 canonical76과 대조하여 subset/중복 없는5를 확인했다. 반송 사본을 읽었으며 Cloud 원본을 직접 조회하지 않았다. R029의 사본 의존/부분 독립성 공개를 유지한다. r01/R025 UNKNOWN은 별도 보존한다. 현재 helper10a·원76 전체·Windows/F6·qualification·Goal PASS로 확대하지 않는다.

## 다음 정확 ID와 원 보장

- test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_carry_rehashes_all_evidence_files_without_freshness_contract — M:3149–3163
- test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_migration_backup_history_idempotence_tamper_cycle_and_readonly — M:756–807
- test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_record_direct_attempt_rejects_task_that_is_not_ready — M:2443–2454
- test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_revalidate_success_rejects_ordinary_pending_task — M:2375–2384
- test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_revision_manifest_rejects_false_carry_and_incomplete_lineage — M:3111–3128

migration 검사는 backup/history/idempotence/read-only digest, same-revision tamper와 DAG cycle/금지 정책 거절을 확인한다. false carry 검사는 task 의미 변경·lineage 누락·중복 task_id를 거절한다. carry-rehash는 FM-00 PASS 뒤 변조한 proof를 다시 hash해 source_succeeded_stale carry를 제외한다. ordinary-pending revalidation은 정확한 legacy reopen event 없이 FM-01 성공을 만들지 못한다.

추가 READY 검사는 M:2443–2454에서 FM-00을 미검토한 채 FM-01을 direct-record한다. helper:6230–6233의 원 READY 오류가 snapshot:6240보다 먼저 발생해야 한다. audit가 금지 효과를 막을 때의 오류는 원 READY regex와 달라 PASS로 삼킬 수 없다. 이는 소스 분기 결론이며 실제 branch observation은 NOT_RUN이다. 성공 direct-record의 Git 경로를 허용하는 변경이 아니다.

## 실제 효과와 허용 fixture

payload는 원 M/helper70f5/legacy 세 파일뿐이다. M:21–35 원 loader와 M:66–104/142–244의 새 TemporaryDirectory, ledger.sqlite3/codex-state.sqlite3, plan/proof/manifest, M:139–140의 원 teardown을 유지한다. M:365–430의 evidence 문구는 원 테스트 fixture이며 실제 업무 승인/운영 검증을 뜻하지 않는다.

원 helper:742–778의 private DB/readonly URI, backup:1435–1452의 backup/atomic rename, register:1913–2070의 SQLite transaction, evidence:5183–5299의 proof hash, review:5319–5455, revalidate:5458–5610, finish:6524–6568의 private DB 처리 경로를 쓴다. 허용 fixture는 새 run-root/tmp 아래뿐이다. 운영 DB/프로필/원장/Git/권한/설치/provider/network를 건드리지 않는다.

기존 r02 SQLite URI와 absolute-private-TMP 검사, import/socket/process/ctypes/chdir 금지는 그대로다. 일반 open/dir_fd를 차단하는 OS sandbox는 없으며 native 후손 전체 coverage를 보장하지 않는다. 정적 named-call 분석은 동적 호출과 모든 branch 효과를 완전히 판정하지 않는다. 새 runtime/SQLite/모듈/실제 효과 관측은 SOURCE/PRE/POST에서 확인해야 한다.

## 미선정/미결의 직접 근거

helper:2089–2106은 비 Git fixture라도 먼저 subprocess.run(git)을 호출한다. 제한 모드는 OSError/CalledProcessError/UnicodeDecodeError만 잡으며 audit RuntimeError는 포함하지 않는다. reserve:3024/3032, direct-record:6240, prepare-direct-selection:6143–6172, M activation fixture:315와 helper _load_activation_decision이 snapshot으로 연결된다. 해당 분기를 무 whitelist/guard 완화나 새 fallback으로 숨기지 않는다. 실제 Git가 필요한 묶음은 정확 root/argv/환경/쓰기·관측 scope 계약을 별도로 준비해야 하며 이번에는 구현하지 않았다. 41개 M 행의 잠재 witness와 행 범위는 JSON에 남겼다.

Windows8 fixture는 WINDIR/System32/cmd.exe(M:1028–1446)를 원 bytes로 요구한다. mock.POpen은 실제 Windows native 관측이 아니다. M:946–976의 실제 cwd extended-prefix와 helper:574–597의 drive/UNC·normcase/abspath 의미를 보존한다. 이미 완료한 C3 문자열 namespace 검사를 재포함하지 않는다.

D:11의 unqualified M fixture import, H:3/I:9의 tests.* import를 그대로 유지해야 한다. 임의 alias/initializer를 붙이지 않는다. D:219–231/254–279는 실제 sys.executable CLI child가 필요하다. I:37–61 bootstrap-only 통합 oracle은 공통 후보지만 원 namespace/origin의 다중 모듈 payload 미준비로 남겼다. D/H/I 나머지의 prepare/record/reserve 효과 분기는 개별 검토가 필요하다.

## r02 대비 변경과 검증

새 runner는 schema/정확 IDs/count 2→5, scope label, 기존 origin7 뒤 command7 추가만 바꿨다. provenance 함수와 journal의 CreateNew/flush/guard 이전 actual 필드/거절 오류 보존은 동일하다. origin14 중 decorator는 open_readonly의 contextlib.contextmanager 하나이며 stdlib binding과 원 helper globals/name/code/path 검증을 그대로 유지한다. SOURCE.DIFF.patch와 STATIC-CHECKS.json에 차이·AST 비교를 남겼다.

실제 검증은 원자료 hash·AST·compile 메모리 확인, 정확76 ID 대조, 완료 사본 해석과 source/ZIP 바이트 대조다. 제품·후보 runner·test import/discovery·dummy·Cloud 실행은0이다. 이전 dummy11이나 C3/U2 PASS는 이번5 runner의 runtime 검증이 아니다. SOURCE/PRE는 Root의 다음 책임이다.

## 원76 전체 대조

원 ID를 그대로 적으며 소스 경로/해시/줄·named 호출·완료 근거·잠재 witness는 ORIGINAL76-INVENTORY.json에 있다.

| 정확 ID | 원 행 | 결과/분류 | 근거 |
|---|---:|---|---|
| test_flowmarshal_direct_selection.DirectSelectionTest.test_active_dispatch_and_expired_lease_preserved | 191–202 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + effect branch 검토 |
| test_flowmarshal_direct_selection.DirectSelectionTest.test_changed_identity_or_schema_rejects_without_rows | 157–168 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + effect branch 검토 |
| test_flowmarshal_direct_selection.DirectSelectionTest.test_default_preserved_opt_in_records_second_and_event | 96–113 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + effect branch 검토 |
| test_flowmarshal_direct_selection.DirectSelectionTest.test_failed_dependency_and_unknown_effect_are_not_bypassed | 126–138 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + effect branch 검토 |
| test_flowmarshal_direct_selection.DirectSelectionTest.test_failed_independent_selection_cannot_complete_workflow | 115–124 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + effect branch 검토 |
| test_flowmarshal_direct_selection.DirectSelectionTest.test_new_failure_after_assignment_requires_new_review | 170–181 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + effect branch 검토 |
| test_flowmarshal_direct_selection.DirectSelectionTest.test_preparation_survives_lease_release_and_records_in_new_run_via_cli | 254–279 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + CLI child |
| test_flowmarshal_direct_selection.DirectSelectionTest.test_ready_tasks_is_read_only_and_cli_option_parses | 219–231 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + CLI child |
| test_flowmarshal_direct_selection.DirectSelectionTest.test_recovery_priority_and_stale_bootstrap_diagnostic | 233–252 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + effect branch 검토 |
| test_flowmarshal_direct_selection.DirectSelectionTest.test_selection_refs_and_required_check_cannot_be_forged | 204–217 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + effect branch 검토 |
| test_flowmarshal_direct_selection.DirectSelectionTest.test_stale_success_and_evidence_tamper_reject | 183–189 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + effect branch 검토 |
| test_flowmarshal_direct_selection.DirectSelectionTest.test_unprepared_and_superseded_preparations_rejected | 140–155 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + effect branch 검토 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_activation_decision_binds_target_spec_and_source_snapshot | 3165–3192 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_capture_create_receipt_accepts_windows_extended_length_cwd | 946–976 | NOT_RUN / WINDOWS_PROFILE_REQUIRED_9 | 원 Windows 경로/cwd |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_capture_create_receipt_binds_actual_full_access_policy | 919–944 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_capture_create_receipt_rejects_workspace_policy_before_confirm | 999–1026 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_carried_task_attempt_invalidates_carry_in_the_reservation_transaction | 3342–3403 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_carry_rehashes_all_evidence_files_without_freshness_contract | 3149–3163 | NOT_RUN / NEXT_COMMON_5 | named snapshot 경로 없음 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_confirm_rejects_create_request_that_differs_from_assignment | 901–917 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_confirm_rejects_legacy_utf8_correction_receipt_v3_without_policy | 1619–1649 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_confirm_rejects_replacement_character_and_legacy_envelope | 1588–1617 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_confirm_rejects_utf8_correction_that_is_not_exact_assignment | 1651–1683 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_confirm_requires_exact_raw_receipt_and_rejects_unverified_client_identity | 1729–1770 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_dependency_freshness_blocks_downstream_ready_after_tamper | 2492–2533 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_draft_does_not_steal_dispatch_binding_and_activation_waits_for_review | 3059–3109 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_evaluation_freshness_contract_blocks_stale_completion | 2135–2227 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_exhausted_recovery_budget_requires_user_decision_in_status_and_registration | 2785–2843 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_expired_lease_and_old_client_binding_preserve_unknown_intent | 1685–1727 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_explicit_no_effect_rejection_requires_review_before_recovery | 2942–3000 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_fail_review_recovery_uses_direct_evidence_and_has_no_deadlock | 2634–2732 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_fm00_gate_one_active_binding_and_completed_is_not_done | 854–899 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_interrupted_turn_requires_bound_provenance_and_never_becomes_failure | 1772–1845 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_json_output_is_ascii_safe_and_round_trips_unicode | 560–567 | PASS_SCOPE_LIMITED / COMPLETED_5 | C3 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_launch_claim_compare_and_set_has_one_winner_under_barrier | 1363–1404 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_launch_claim_survives_lease_takeover_and_blocks_all_relaunches | 1406–1446 | NOT_RUN / WINDOWS_PROFILE_REQUIRED_9 | 원 Windows 경로/cwd |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_launch_codex_exec_rechecks_lease_after_streams_open_before_popen | 1156–1194 | NOT_RUN / WINDOWS_PROFILE_REQUIRED_9 | 원 Windows 경로/cwd |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_launch_codex_exec_recovers_existing_launch_without_relaunch | 1275–1327 | NOT_RUN / WINDOWS_PROFILE_REQUIRED_9 | 원 Windows 경로/cwd |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_launch_codex_exec_rejects_claimless_receipt_and_direct_confirm | 1196–1236 | NOT_RUN / WINDOWS_PROFILE_REQUIRED_9 | 원 Windows 경로/cwd |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_launch_codex_exec_rejects_multiple_thread_started_identities | 1329–1361 | NOT_RUN / WINDOWS_PROFILE_REQUIRED_9 | 원 Windows 경로/cwd |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_launch_codex_exec_requires_one_thread_started_for_existing_receipt | 1238–1273 | NOT_RUN / WINDOWS_PROFILE_REQUIRED_9 | 원 Windows 경로/cwd |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_launch_codex_exec_retries_transient_policy_observation | 1101–1154 | NOT_RUN / WINDOWS_PROFILE_REQUIRED_9 | 원 Windows 경로/cwd |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_launch_codex_exec_uses_explicit_full_access_and_captures_v4_receipt | 1028–1099 | NOT_RUN / WINDOWS_PROFILE_REQUIRED_9 | 원 Windows 경로/cwd |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_legacy_digest_contract_revalidates_already_stored_success_evidence | 2535–2575 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_migration_backup_history_idempotence_tamper_cycle_and_readonly | 756–807 | NOT_RUN / NEXT_COMMON_5 | named snapshot 경로 없음 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_policy_mismatch_reopens_same_task_without_duplicate_recovery | 1498–1586 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_record_direct_attempt_completes_ready_task_without_dispatch | 2394–2441 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_record_direct_attempt_rejects_bootstrap_and_active_dispatch | 2456–2466 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_record_direct_attempt_rejects_task_that_is_not_ready | 2443–2454 | NOT_RUN / NEXT_COMMON_5 | READY 사전 거절 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_record_direct_attempt_with_finding_marks_task_failed | 2468–2490 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_registered_multistage_dag_and_specs_are_the_only_next_stage_authority | 2577–2632 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_reopen_historical_interruption_is_append_only_idempotent_and_reserves_attempt_two | 1847–1948 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_reopen_historical_interruption_rejects_forged_known_origin | 1998–2048 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_reopen_historical_interruption_rejects_non_recovery_and_active_dispatch | 2050–2080 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_reopen_historical_interruption_rejects_receipt_and_decision_binding_mismatches | 1950–1996 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_reserve_enforces_main_checkout_before_creating_attempt | 2900–2940 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_revalidate_success_rejects_forged_legacy_reopen_event | 2287–2373 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_revalidate_success_rejects_ordinary_pending_task | 2375–2384 | NOT_RUN / NEXT_COMMON_5 | named snapshot 경로 없음 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_revalidate_success_repairs_legacy_reopen_without_dispatch | 2229–2285 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_revision5_requires_typed_ancestor_inputs_and_evidence_contract | 809–852 | PASS_SCOPE_LIMITED / COMPLETED_5 | C3 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_revision_draft_is_inert_then_activation_is_atomic_and_cas_guarded | 3003–3057 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_revision_manifest_rejects_false_carry_and_incomplete_lineage | 3111–3128 | NOT_RUN / NEXT_COMMON_5 | named snapshot 경로 없음 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_revision_registration_and_activation_honor_manifest_guard | 3130–3147 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_revision_two_can_rollback_before_its_first_attempt_or_dispatch | 3276–3309 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_revision_two_history_permanently_blocks_rollback_to_revision_one | 3311–3340 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_revision_two_recovery_survives_verify_first_and_is_reserved_next_tick | 2734–2783 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_same_path_accepts_only_drive_and_unc_extended_namespaces | 978–997 | PASS_SCOPE_LIMITED / COMPLETED_5 | C3 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_schema_v3_migrates_additively_from_v2_and_preserves_history | 3194–3233 | PASS_SCOPE_LIMITED / COMPLETED_5 | U2 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_schema_v3_writer_fence_rejects_missing_and_old_contract_versions | 3235–3274 | PASS_SCOPE_LIMITED / COMPLETED_5 | U2 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_task_reasoning_policy_starts_medium_and_escalates_after_reviewed_failure | 2082–2133 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_terminal_observation_is_idempotent_and_never_regresses_to_running | 1448–1496 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_verify_rejects_orphaned_recovery_marker | 2858–2898 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_implementation_workflow.ImplementationWorkflowTest.test_verify_rejects_same_count_wrong_lineage_tuple | 2845–2856 | NOT_RUN / EFFECT_BRANCH_REVIEW_41 | snapshot→helper2095, branch 미결 |
| test_flowmarshal_same_tick_handoff.SameTickHandoffTest.test_terminal_review_run_can_reserve_exactly_one_next_ready_task | 20–82 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + effect branch 검토 |
| test_flowmarshal_workflow_integration.WorkflowIntegrationTest.test_expired_owner_is_fenced_and_same_evidence_cannot_repeat_recovery | 108–132 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + effect branch 검토 |
| test_flowmarshal_workflow_integration.WorkflowIntegrationTest.test_missing_tampered_and_unrun_checks_cannot_complete_bootstrap | 37–61 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + effect branch 검토 |
| test_flowmarshal_workflow_integration.WorkflowIntegrationTest.test_recovery_success_revalidates_original_new_attempt_then_completes | 63–106 | NOT_RUN / MODULE_IDENTITY_REVIEW_16 | 원 import/origin + effect branch 검토 |
