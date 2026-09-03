# Native Windows 외부 읽기 임시 예외 감사 기록

## 결론

FlowMarshal의 목표 정책은 그대로다. 사용자가 등록한 외부 자료만 읽고, 등록하지 않은 경로는 차단하는 것이 최종 상태다.

다만 현재 native Windows permission profile은 `:root=deny`를 설정해도 상속된 읽기 권한을 이용한 미등록 외부 파일 읽기를 막지 못한다. 이 문제는 고정 런타임 0.147.0과 최신 교차검증 런타임 0.152.0에서 모두 재현됐다. 따라서 이 읽기 한 항목만 **조건부 임시 호환성 예외**로 승인한다.

이 결정은 “외부 파일은 마음대로 찾아 읽어도 된다”는 뜻이 아니다. 외부 로그·요구사항·참고 자료는 여전히 프로젝트 입력으로 등록하고, 경로·출처·digest와 사용 task를 기록해야 한다. 현재 sandbox가 그 계약을 OS 수준에서 완전히 강제하지 못한다는 사실을 정직하게 기록하는 것이다.

## 현재 보호 경계

임시 예외는 미등록 외부 경로의 읽기에만 적용한다. 다음 항목은 계속 필수 차단 조건이며, 하나라도 깨지면 Gate 0A-P는 실패다.

- 작업 루트 밖 쓰기
- 승인된 외부 참조 쓰기
- FlowMarshal 보호 경로 읽기와 쓰기
- 읽기 전용 제어 파일 쓰기
- sandboxed command 네트워크
- Web Search, MCP, Plugin, Connector, Browser/Computer Use 표면
- 서버 승인 요청

따라서 현재 native Windows sandbox는 파일 내용의 완전한 비밀 보장 장치로 주장하지 않는다. 작업 공간 밖의 사용자 데이터를 훼손하거나 외부로 전송하지 못하게 하는 손상 방지 경계로 사용한다.

## 향후 보완 방법

엄격 설정과 검증 카나리는 제거하지 않는다.

1. permission profile의 `:root=deny`를 계속 설정한다.
2. 등록된 외부 참조의 명시적 읽기 허용을 유지한다.
3. 미등록 외부 파일 읽기 카나리를 매 지원 런타임 검증에서 계속 실행한다.
4. 카나리가 `strict_read_isolation`으로 바뀌면 임시 예외가 사용되지 않은 것으로 판정한다.
5. 그 결과를 근거로 별도 정책 개정을 거쳐 임시 예외를 제거한다.
6. 통신 오류처럼 엄격 차단인지 호환성 읽기인지 알 수 없는 결과는 실패 처리한다.

즉, 예외는 날짜가 되면 자동 연장되는 상시 허용이 아니다. **지원 Codex 런타임 변경 또는 카나리의 엄격 차단 전환**이 재검토 조건이다.

## 검증 증거

- 고정 런타임 0.147.0: `fm0ap-live-20260901-11-approved-read-exception`, 전체 통과, 읽기 모드 `temporary_native_windows_broad_read`
  - `spikes/gate0a/artifacts/permission-rechecks/fm0ap-live-20260901-11-approved-read-exception/attempt-result.json`
  - SHA-256 `26ff798bfb3a576b233d84f9b3b21c4d38d86834f301e7353f0d61ca40305ea4`
- 최신 런타임 0.152.0 교차검증: `fm0ap-research-20260901-12-codex-0152-approved-exception`, 기록 전용 전체 통과, 같은 읽기 모드
  - `spikes/gate0a/artifacts/permission-rechecks/fm0ap-research-20260901-12-codex-0152-approved-exception/attempt-result.json`
  - SHA-256 `a66129cbab57b1cd5be895b164f89d57b622537c64f649d1070447760ac5ca21`
- schema 1.2 원본과 판정은 schema 1.3 변환 전에 `spikes/gate0a/artifacts/legacy/`에 보존했다.

첫 재검증 `fm0ap-live-20260901-10-approved-read-exception`은 수동으로 잘못 입력한 존재하지 않는 Desktop task ID 때문에 외부 표면 확인만 실행 오류가 났다. 이 실패 기록도 삭제하지 않았다. 올바른 기존 task ID를 사용한 다음 시도에서 전체 검사를 통과했다.

## 공식 기준

- [Codex sandboxing](https://learn.chatgpt.com/docs/sandboxing)
- [Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)
- [Permissions](https://learn.chatgpt.com/docs/permissions)
- [App Server](https://learn.chatgpt.com/docs/app-server)
- [Changelog](https://learn.chatgpt.com/docs/changelog)
