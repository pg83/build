#!/usr/bin/env python3

import importlib.util
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("build_style", ROOT / "style.py")
style = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(style)


class StyleTest(unittest.TestCase):
    def test_source_files_include_objective_cxx(self):
        with mock.patch.object(
            style.subprocess,
            "check_output",
            return_value=b"platform.mm\0",
        ) as check_output:
            self.assertEqual(style.source_files([]), [ROOT / "platform.mm"])

        check_output.assert_called_once_with(
            [
                "git",
                "ls-files",
                "-z",
                "--",
                "*.cpp",
                "*.h",
                "*.mm",
            ],
            cwd=ROOT,
        )


if __name__ == "__main__":
    unittest.main()
