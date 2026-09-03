# 폐기 Windows VM 가용성 점검

- 점검 결과: **준비된 폐기 Windows VM 없음**
- 호스트 변경: **없음**
- 비밀 파일 내용 열람: **없음**

Windows 11 Pro 호스트에서 Hyper-V 명령, Windows Sandbox, VirtualBox, VMware,
Multipass와 기존 Hyper-V VM을 읽기 전용으로 확인했다. 현재 바로 실행 가능한 Windows
VM 수단은 없었다. Docker CLI와 WSL2의 `docker-desktop` 항목은 있으나 daemon은
중지 상태였으며, 이들은 Windows 로컬 샌드박스 사용자와 방화벽 규칙을 포함한 컴퓨터
전체 provisioning 검사를 대체할 수 없다.

Windows 선택 기능 상태 조회에는 관리자 권한이 필요했다. 기능 활성화, hypervisor
설치, VM 생성, 재부팅은 수행하지 않았다. 따라서 VM 증거가 실제로 생성될 때까지
Gate0A-P와 Gate0B 전체 판정은 `NO-GO`로 유지한다.
