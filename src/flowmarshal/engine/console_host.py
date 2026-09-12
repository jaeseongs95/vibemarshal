"""대화형 사용자 승인을 process-local Core capability로 연결하는 console host."""
from __future__ import annotations

import json
import sys
from typing import TextIO

from pydantic import ValidationError

from . import cli
from .application import ApplicationAuthority, EngineApplicationError
from .capabilities import GoalAuthorizationTarget, require_host_execution
from .ledger import EngineLedgerError
from .service import EngineServiceError


class TrustedConsoleHost:
    """표시한 Goal 경계를 사용자가 확인한 경우에만 일회성 handle을 발급한다."""

    def __init__(
        self,
        *,
        input_stream: TextIO | None = None,
        approval_stream: TextIO | None = None,
    ) -> None:
        self.input_stream = input_stream or sys.stdin
        self.approval_stream = approval_stream or sys.stderr

    def run(self, argv: list[str] | None = None) -> int:
        # role callback이 새 host/authority를 만들기 전에 재진입 자체를 차단한다.
        require_host_execution()
        raw_argv = list(sys.argv[1:] if argv is None else argv)
        arguments = cli.build_parser().parse_args(raw_argv)
        if getattr(arguments, "trusted_action", None) != "goal.authorize":
            return cli._execute_parsed(arguments, raw_argv=raw_argv)
        try:
            return self._authorize(arguments)
        except (
            EngineServiceError,
            EngineApplicationError,
            EngineLedgerError,
            ValidationError,
            ValueError,
            OSError,
            KeyError,
        ) as error:
            prefix = str(error).split(":", 1)[0]
            code = getattr(error, "code", None) or (
                prefix if prefix.isupper() and " " not in prefix else None
            )
            cli._emit({"error": type(error).__name__, "error_code": code, "message": str(error)})
            return 2

    def _authorize(self, arguments) -> int:
        policy = cli._goal_authorization_policy(arguments)
        application_authority = ApplicationAuthority(cli._application(arguments))
        target = application_authority.authorization_target(
            arguments.project_id,
            operating_policy=policy,
        )
        self._show_target(target, audit_source=arguments.source)
        if not self._is_interactive():
            cli._emit({
                "status": "confirmation_required",
                "error_code": "AUTHORIZATION_CONFIRMATION_REQUIRED",
                "target_digest": target.target_digest,
            })
            return 2
        confirmation = self.input_stream.readline()
        # Enter의 줄바꿈만 제외한다. 공백이나 다른 문자를 정규화해 승인하지 않는다.
        if confirmation == "" or confirmation.removesuffix("\n").removesuffix("\r") != target.target_digest:
            cli._emit({
                "status": "declined",
                "error_code": "AUTHORIZATION_DECLINED",
                "target_digest": target.target_digest,
            })
            return 1

        result = application_authority.authorize(
            arguments.project_id,
            target=target,
            source=arguments.source,
            operating_policy=policy,
        )
        cli._emit(result)
        return 0

    def _show_target(self, target: GoalAuthorizationTarget, *, audit_source: str) -> None:
        value = target.model_dump(mode="json")
        value["budget_policies"] = [json.loads(item) for item in target.budget_policies]
        value["target_digest"] = target.target_digest
        value["audit_source"] = audit_source
        self.approval_stream.write("FlowMarshal Goal 승인 대상:\n")
        self.approval_stream.write(json.dumps(value, ensure_ascii=False, indent=2))
        self.approval_stream.write(
            "\n승인하려면 위 target_digest 전체를 그대로 입력하세요. "
            "다른 입력은 거절로 처리합니다.\n> "
        )
        self.approval_stream.flush()

    def _is_interactive(self) -> bool:
        isatty = getattr(self.input_stream, "isatty", None)
        return bool(isatty and isatty())


def main(argv: list[str] | None = None) -> int:
    return TrustedConsoleHost().run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
