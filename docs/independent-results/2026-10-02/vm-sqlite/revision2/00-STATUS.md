# 보존된 revision 2: 통합 미승인 / revision 3로 대체

이 폴더의 REPORT-V2.md와 로그는 당시 검증 결과를 변경 없이 보존한 기록이다. 보고서 작성 후 독립 감사가 malformed SQLite 파일에서 연결 설정 PRAGMA 실패 시 acquired connection이 닫히지 않는 cleanup 회귀를 확인했다. 따라서 revision 2는 통합 미승인이며 현재 제안은 별도 revision 3이다.

- revision 2 patch: 47701107ba6f78135badc01da6353b62c2ee5429777ac4844a7c5b447e117a7c
- revision 3 patch: 40339ffa0ef57f5d6152c289344c7fda0011c70c0ea40338861495c3fa170a89
- canonical source: NOT_APPLIED
- 이 상태 공지는 과거 원시 실패·성공 evidence를 덮어쓰지 않는다
