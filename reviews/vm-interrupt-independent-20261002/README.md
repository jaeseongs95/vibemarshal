# 게시 준비 상태

이 폴더는 부모 작업의 검토용으로 준비한 독립 기술 검토 packet이다. 부모의 명시 승인으로 비공개 브랜치 `dot/vm-interrupt-review-20261002`에 이 증거만 기록한다. repository prefix는 `reviews/vm-interrupt-independent-20261002/`이며 기준은 exact `32bb0f9dd9f024045d24487312b50f5b703573a3`이다. Library upload는 없다. product source·merge·integration 승인이 아니다.

먼저 [REPORT.ko.md](REPORT.ko.md)를 읽는다. R01은 명시 계약과 새 hardening 정책의 구분, R02는 claim/send crash와 재시도 소실, R03은 owner 연결·no-effect 거절·exact target·reconciliation 경계다. `evidence/`에는 exact 원본 명령을 경로 alias로 표현한 command receipts, before/after synthetic observations, 원격 artifact/hash 검증과 원시 로그가 있다. exit0은 관측 oracle 일치이며 product PASS가 아니다.

`MANIFEST.json`은 원본 bytes와 게시 준비 bytes의 SHA-256을 구분한다. 경로 치환은 `<REVIEW_ROOT>`=이 검토 task 임시 root, `<PYTHON_ENV>`=고정 Python 환경, `<BASE_CHECKOUT>`=공유 기준 checkout이다. JSON command receipt의 `original_stdout_sha256`·`original_stderr_sha256`는 실행 시 bytes, `normalized_*`는 이 packet의 정규화한 로그 bytes다. 운영 경로·개인 경로·DB/WAL·provider profile/key·session·인증정보는 넣지 않았다.

재현할 때는 이 Python script들을 새 `<REVIEW_ROOT>` 루트에 복사하고 exact `32bb0f9dd9f024045d24487312b50f5b703573a3` source를 `base/`, `candidate/`로 각각 복제한다. 제안 commit `6c28f9f271f4e99e1ee81c05db75dc342a022558`의 inert patch만 candidate에 적용한다. script의 `HERE`는 해당 임시 root이며 `scratch/`가 없는 새 위치가 필요하다. main·idempotent·close runner는 별도 scratch case명을 사용한다. fixed Python으로 각각 source별 runner를 실행하면 synthetic state만 생성한다. 기존 suite를 반복하거나 live provider/AGS를 부르지 않는다.

artifact 재대조 runner를 다시 쓰려면 제안의 30파일을 `packet/`에 가져오고, 각 GitHub base64 response를 `{path,data}` 배열 형태 `remote-packet.json`으로 제공해야 한다. 이 API 원문 packet은 중복 전달하지 않았다. `verify_inputs.py`의 `<BASE_CHECKOUT>` 경로와 Python 환경 alias를 재현자의 실제 경로로 복원한다. source file hashes·실행 metadata와 export manifest를 새로운 결과로 기록하며 이번 원시 결과를 덮어쓰지 않는다.

공유 source는 수정하지 않았다. SQLite·Goal-verdict proposal을 candidate에 적용하지 않았고 F07·consumed FM03·Windows/provider qualification도 수행하지 않았다.
