# 별도 폐기 Windows VM 실행 결정

- 선택 환경: 별도 폐기 Windows VM
- 현재 상태: `READY_FOR_VM_TRANSFER`
- 이 PC의 Windows Sandbox·Hyper-V 변경: 없음
- 이 PC의 `windowsSandbox/setupStart` 호출: 없음
- 연결된 Codex 호스트: 이 PC(`local`)만 확인됨
- 실제 VM 생성·원격 실행: 접속 대상 또는 승인된 공급자·예산이 없어 아직 시작하지 않음

## 준비된 전달물

- ZIP: `handoff/flowmarshal-external-windows-vm-20260902-100047.zip`
- SHA-256: `a01f2103d2595d59ff218ff896ff5166ab3ca444ebc2dcbff206f58634e866b7`
- 검증 결과: `VALID` (28개 허용 파일)
- Codex 홈·인증정보·sandbox secrets 포함 여부: 모두 없음

번들에는 관리자·VM 식별 사전검사, base snapshot별 세 시나리오 실행, 제한된 재시도,
JSON 증거만 반출하는 manifest 검사, 호스트 가져오기와 폐기 작업 ID 연결 스크립트가
포함돼 있다.

## 다음 실행 조건

다음 중 하나가 제공되면 실제 VM 단계로 진행할 수 있다.

1. 기존 Windows VM의 SSH/WinRM 접속 방법
2. 연결된 별도 Codex Windows 호스트
3. 사용할 VM 공급자, 구독·프로젝트, 허용 예산과 폐기 정책
4. VM 운영자가 번들을 실행한 뒤 반환한 증거 공유 폴더

실제 suite와 VM 폐기 증거가 없으므로 Gate0A-P와 전체 Gate0A는 `NO-GO`, 전체
Gate0B는 `BLOCKED/NO-GO`를 유지한다.
