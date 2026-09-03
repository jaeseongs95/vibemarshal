# 로컬 VirtualBox 폐기 Windows VM 완료 기록

- 상태: **완료**
- 공급자: Oracle VirtualBox `7.2.16`
- 호스트 Windows Sandbox·Hyper-V 기능 변경: 없음
- 호스트 Codex의 `windowsSandbox/setupStart` 호출: 없음
- VM: `FlowMarshal-Disposable-Win11`
- VM UUID: `246a5d7a-af7f-49cc-8f30-5efa6351d917`
- suite: `fm-sbx-vm-20260902-05` / **GO**
- 폐기: `VBoxManage unregistervm --delete` 완료, 등록 VM·가상 디스크·설정 파일 잔존 `0`

공식 Windows 11 IoT Enterprise LTSC 2024 Evaluation x64 ISO로 호스트와 분리된 VM을 만들고 현재 Codex 최초 설정, `0.147.0 → 0.151.0` 교차버전, 두 번째 authoritative home 차단, 주입 실패 뒤 제한 재시도, artifact 안전성과 폐기를 검증했다. [suite 결과](suites/fm-sbx-vm-20260902-05/suite-result.json)가 Gate 복구의 권위 증거다.

VirtualBox, 평가판 ISO와 비밀정보 없는 재사용 payload는 다음 재검증을 위해 남겼다. 최종 임시 평문·DPAPI 자격증명 파일은 삭제해 잔존 비밀 파일 수는 `0`이다. 설치 과정의 진단 화면 18개는 Gate 증거가 아니며 VM 작업 폴더에 남아 있다.

VirtualBox unattended dry-run 출력에 최초 임시 암호가 한 번 표시됐지만 실제 설치 전에 회전해 그 값은 실제 VM에 사용되지 않았다. 사용된 임시 자격증명 파일과 VM 자체는 모두 폐기했다.
