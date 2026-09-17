"""Engine-only wheel에 필요한 Python module만 선택한다.

source tree의 legacy/prototype module은 보존하지만, setuptools 기본 탐색은
``flowmarshal`` 최상위 package 아래의 모든 .py를 wheel에 넣는다. 이 hook은
최상위에서 canonical helper만 남겨 그 우연한 재포장을 막는다.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _BuildPy


ENGINE_DEVELOPER_MODULES = {
    "benchmark",
    "benchmark_candidate_costs",
    "benchmark_lifecycle",
    "benchmark_observation",
    "benchmark_safety",
    "e2e_qualification",
    "eval_cli",
    "evaluation",
    "evaluation_budget",
    "freeze",
    "inspection_diagnostic_budget",
    "performance",
    "performance_assessment",
    "performance_lifecycle_safety",
    "qualification",
    "scope_report_verification",
    "smoke",
}


class EngineOnlyBuildPy(_BuildPy):
    def run(self) -> None:
        # 이전 broad wheel build가 남긴 legacy module은 현재 package discovery가
        # 제외해도 build/lib에 잔존할 수 있다. staging output만 매 build 전에
        # 비워 wheel 내용도 discovery 규칙과 동일하게 만든다.
        shutil.rmtree(Path(self.build_lib) / "flowmarshal", ignore_errors=True)
        super().run()

    def find_package_modules(self, package: str, package_dir: str):  # type: ignore[no-untyped-def]
        modules = super().find_package_modules(package, package_dir)
        if package == "flowmarshal":
            return [
                item
                for item in modules
                if item[1] in {"__init__", "canonical", "time"}
            ]
        if package == "flowmarshal.engine":
            return [item for item in modules if item[1] not in ENGINE_DEVELOPER_MODULES]
        return modules


setup(cmdclass={"build_py": EngineOnlyBuildPy})
