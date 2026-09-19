"""플러그인 없이 선언 밖 파일 변경을 막는 결정적 task gate(방법론만 채택 검증용 spike).

Worker dispatch 직전에 프로젝트 파일 전체의 내용 digest를 기록하고, Task 완료 직전에 다시 읽어
바뀐 경로가 Execution Spec의 쓰기 target 밖에 있으면 완료를 막는다. 모델을 호출하지 않는다.
완료 판정은 FlowMarshal Core가 한다. gate는 차단 사유만 돌려준다.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from flowmarshal.engine.context import DEFAULT_IGNORED_DIRECTORIES
from flowmarshal.engine.domain import TaskExecutionSpecRevision


def workspace_digests(root: Path, excluded: tuple[Path, ...] = ()) -> dict[str, str]:
    """Engine 경로 inventory와 같은 무시 규칙으로 파일별 sha256을 모은다."""
    digests: dict[str, str] = {}
    for directory, directories, filenames in os.walk(root):
        current = Path(directory)
        directories[:] = [name for name in directories if name.casefold() not in DEFAULT_IGNORED_DIRECTORIES
                          and (current / name).resolve() not in excluded]
        for name in filenames:
            path = current / name
            if path.is_file():
                digests[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


class WorkspaceScopeGate:
    def __init__(self, service: Any) -> None:
        self.service = service
        # ponytail: 기준선은 메모리에만 둔다. Engine 재시작 뒤 이어 가려면 원장 evidence로 옮긴다.
        self.baselines: dict[str, dict[str, str]] = {}

    def _inputs(self, task: Any) -> tuple[Path, set[str]]:
        with self.service.ledger.read() as connection:
            spec = connection.execute("SELECT payload_json FROM execution_spec_revisions WHERE task_id = ? "
                                      "AND is_current = 1", (task["id"],)).fetchone()
            project = connection.execute("SELECT root FROM projects WHERE id = ?", (task["project_id"],)).fetchone()
        targets = TaskExecutionSpecRevision.model_validate_json(spec["payload_json"]).definition.resolved_targets
        return Path(project["root"]).resolve(), {item.path for item in targets if item.access != "read"}

    def _snapshot(self, root: Path) -> dict[str, str]:
        return workspace_digests(root, (self.service.ledger.artifact_root.resolve(),))

    def before_execution(self, task: Any) -> str | None:
        if task["id"] not in self.baselines:
            self.baselines[task["id"]] = self._snapshot(self._inputs(task)[0])
        return None

    def before_completion(self, task: Any) -> str | None:
        baseline = self.baselines.get(task["id"])
        if baseline is None:
            return "WORKSPACE_BASELINE_MISSING: Worker dispatch 전 기준선이 없습니다."
        root, writes = self._inputs(task)
        current = self._snapshot(root)
        outside = sorted(path for path in baseline.keys() | current.keys()
                         if baseline.get(path) != current.get(path) and path not in writes)
        if outside:
            return "UNDECLARED_CHANGE: 쓰기 target 밖 변경 " + ", ".join(outside)
        return None
