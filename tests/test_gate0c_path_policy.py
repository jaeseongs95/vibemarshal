from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from flowmarshal.path_policy import (
    PathDecisionCode,
    PathPolicyError,
    assert_distinct_resources,
    decide_resource,
    inspect_resource,
    lexical_windows_path,
    revalidate_unchanged,
)


class Gate0CPathParserTests(unittest.TestCase):
    def test_accepts_normal_korean_space_and_long_lexical_path(self) -> None:
        value = "d:\\합성 작업\\공백 폴더\\" + ("긴이름" * 100)
        parsed = lexical_windows_path(value)
        self.assertTrue(parsed.startswith("D:\\합성 작업"))
        self.assertGreater(len(parsed), 260)

    def test_rejects_traversal_ads_device_unc_and_ambiguous_names(self) -> None:
        cases = {
            "D:\\safe\\..\\outside": PathDecisionCode.PATH_TRAVERSAL,
            "D:\\safe\\file.txt:secret": PathDecisionCode.PATH_ADS,
            "\\\\.\\PhysicalDrive0": PathDecisionCode.UNSUPPORTED_PATH_CLASS,
            "\\\\?\\D:\\safe": PathDecisionCode.UNSUPPORTED_PATH_CLASS,
            "\\??\\D:\\safe": PathDecisionCode.UNSUPPORTED_PATH_CLASS,
            "\\\\server\\share\\safe": PathDecisionCode.UNSUPPORTED_PATH_CLASS,
            "D:\\safe\\name. ": PathDecisionCode.PATH_TRAILING_DOT_SPACE,
            "D:\\safe\\CON.txt": PathDecisionCode.PATH_RESERVED_NAME,
            "relative\\file.txt": PathDecisionCode.PATH_NOT_ABSOLUTE,
            "D:\\safe\\bad?.txt": PathDecisionCode.PATH_INVALID_CHARACTER,
        }
        for value, expected in cases.items():
            with self.subTest(value=value), self.assertRaises(PathPolicyError) as caught:
                lexical_windows_path(value)
            self.assertEqual(expected, caught.exception.reason_code)


@unittest.skipUnless(os.name == "nt", "native Windows handle 검사가 필요합니다.")
class Gate0CWindowsPathPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="flowmarshal-gate0c-")
        self.root = Path(self.temp.name) / "한글 workspace"
        self.root.mkdir()
        (self.root / "normal.txt").write_text("정상", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_normal_case_alias_and_manifest_are_allowed(self) -> None:
        first = inspect_resource(self.root)
        alias = str(self.root).swapcase()
        second = inspect_resource(alias)
        self.assertEqual(first.root_identity.object_key, second.root_identity.object_key)
        self.assertEqual(first.manifest_digest, second.manifest_digest)
        self.assertTrue(decide_resource(self.root).allowed)

    def test_real_long_path_is_allowed(self) -> None:
        current = self.root
        created: list[Path] = []
        for index in range(8):
            current = current / (f"긴경로{index}-" + "가" * 28)
            os.mkdir("\\\\?\\" + str(current))
            created.append(current)
        final_file = current / "끝.txt"
        with open("\\\\?\\" + str(final_file), "w", encoding="utf-8") as stream:
            stream.write("long")
        try:
            decision = decide_resource(self.root)
            self.assertTrue(decision.allowed, decision.message)
        finally:
            os.unlink("\\\\?\\" + str(final_file))
            for directory in reversed(created):
                os.rmdir("\\\\?\\" + str(directory))

    def test_hardlink_and_ads_are_rejected(self) -> None:
        source = self.root / "normal.txt"
        link = self.root / "hardlink.txt"
        os.link(source, link)
        decision = decide_resource(self.root)
        self.assertFalse(decision.allowed)
        self.assertEqual(PathDecisionCode.PATH_HARDLINK, decision.reason_code)
        link.unlink()

        ads_path = Path(str(source) + ":attack")
        ads_path.write_text("attack", encoding="utf-8")
        decision = decide_resource(self.root)
        self.assertFalse(decision.allowed)
        self.assertEqual(PathDecisionCode.PATH_ADS, decision.reason_code)

    def test_junction_is_rejected(self) -> None:
        target = Path(self.temp.name) / "outside"
        target.mkdir()
        junction = self.root / "junction"
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest(f"junction fixture를 만들 수 없음: {completed.stderr}")
        try:
            decision = decide_resource(self.root)
            self.assertFalse(decision.allowed)
            self.assertEqual(PathDecisionCode.PATH_REPARSE_POINT, decision.reason_code)
        finally:
            os.rmdir(junction)

    def test_outside_root_and_resource_identity_collision_are_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        with self.assertRaises(PathPolicyError) as caught:
            inspect_resource(outside, allowed_root=self.root)
        self.assertEqual(PathDecisionCode.PATH_OUTSIDE_ROOT, caught.exception.reason_code)

        inspection = inspect_resource(self.root)
        with self.assertRaises(PathPolicyError) as caught:
            assert_distinct_resources((inspection, inspection))
        self.assertEqual(PathDecisionCode.PATH_IDENTITY_CONFLICT, caught.exception.reason_code)

    def test_dispatch_revalidation_detects_content_and_root_replacement(self) -> None:
        first = inspect_resource(self.root)
        (self.root / "normal.txt").write_text("변경", encoding="utf-8")
        with self.assertRaises(PathPolicyError) as caught:
            revalidate_unchanged(first)
        self.assertEqual(PathDecisionCode.PATH_IDENTITY_DRIFT, caught.exception.reason_code)

        fresh = inspect_resource(self.root)
        moved = Path(self.temp.name) / "moved"
        self.root.rename(moved)
        self.root.mkdir()
        (self.root / "normal.txt").write_text("변경", encoding="utf-8")
        with self.assertRaises(PathPolicyError) as caught:
            revalidate_unchanged(fresh)
        self.assertEqual(PathDecisionCode.PATH_IDENTITY_DRIFT, caught.exception.reason_code)


if __name__ == "__main__":
    unittest.main()
