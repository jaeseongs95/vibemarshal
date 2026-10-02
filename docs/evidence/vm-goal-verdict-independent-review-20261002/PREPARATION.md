# evidence branch 준비 검증

parent의 follow-up에 따라 original review artifact를 historical bytes로 보존하고, 새 README와 최소 compatibility counterexample/required assertions만 추가했다. 제품/test source는 변경하지 않았다. 테스트 재실행·provider 추가 호출은 0회다. 원격 push 없이 로컬 branch를 준비한다.

- original REVIEW.md SHA-256: `35b85037bf961d343000e30fa4c2fbfa1c03a66c53a75175ce5919f2ea566c60`.
- original inventory와 SHA256SUMS에 기록된 모든 copied file의 hash를 검증했다.
- staged allowlist는 `docs/evidence/vm-goal-verdict-independent-review-20261002/` 하나다. DB·archive source tree·fixture·operational profile/keys를 포함하지 않았다.
- 기본 `git diff --cached --check`는 historical `public_fixture.py` 마지막 빈 줄을 `new blank line at EOF`로 지적했다. 해당 파일은 original SHA-256을 보존하려고 수정하지 않았다.
- `git -c core.whitespace=-blank-at-eof diff --cached --check`는 exit 0이었다. EOF 빈 줄 검사만 제외하며 trailing spaces 등 다른 whitespace 검사는 유지한다. 이 차이는 새 제품 코드의 검증 예외가 아니라 original evidence byte 보존을 위한 명시적 artifact 검사 범위다.
- 새 candidate는 미수신이다. 다음 independent re-review는 변경된 policy 경계·관련 regression에 한정하고 original report를 덮어쓰지 않는다.
