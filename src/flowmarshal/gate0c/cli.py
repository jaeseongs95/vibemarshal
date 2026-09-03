from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .verifier import Gate0CVerifier, write_report


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flowmarshal-gate0c",
        description="FlowMarshal Gate 0C 내부 독립 검증 CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="원시 원장과 artifact를 독립 검증합니다.")
    verify.add_argument("--project-root", type=Path, default=_default_project_root())
    verify.add_argument("--db", type=Path)
    verify.add_argument("--profile-artifact", type=Path)
    verify.add_argument("--results", type=Path)
    verify.add_argument("--markdown", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "verify":
        raise AssertionError(f"지원하지 않는 command: {args.command}")
    project_root = args.project_root.resolve(strict=True)
    artifact_root = project_root / "spikes" / "gate0c" / "artifacts"
    results = args.results or artifact_root / "gate0c-results.json"
    markdown = args.markdown or artifact_root / "gate0c-report.md"
    report = Gate0CVerifier(
        project_root=project_root,
        database_path=args.db,
        profile_artifact_path=args.profile_artifact,
    ).verify()
    write_report(report, json_path=results, markdown_path=markdown)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0 if report.overall == "GO" else 2


if __name__ == "__main__":
    raise SystemExit(main())
