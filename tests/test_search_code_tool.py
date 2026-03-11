import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import main as agent_main


class SearchCodeToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self._temp_dir.name).resolve()
        self.original_workspace = agent_main.WORKSPACE_DIR
        agent_main.WORKSPACE_DIR = self.workspace
        self.tool = agent_main.SearchCodeTool()

    def tearDown(self) -> None:
        agent_main.WORKSPACE_DIR = self.original_workspace
        self._temp_dir.cleanup()

    def write_file(self, relative_path: str, content: str) -> None:
        file_path = self.workspace / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

    def run_tool(self, parameters):
        return json.loads(self.tool.run(parameters))

    def build_match_event(self, relative_path: str, line_number: int, snippet: str) -> str:
        return json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": str((self.workspace / relative_path).resolve())},
                    "lines": {"text": snippet + "\n"},
                    "line_number": line_number,
                },
            },
            ensure_ascii=False,
        )

    @patch("main.shutil.which", return_value="rg")
    @patch("main.subprocess.run")
    def test_search_fixed_string_returns_relative_path_and_line(self, mock_run, _: object) -> None:
        self.write_file(
            "src/example.py",
            "def helper():\n    pass\n\n\ndef target_function():\n    return 1\n",
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=self.build_match_event(
                "src/example.py",
                5,
                "def target_function():",
            ),
            stderr="",
        )

        result = self.run_tool({"query": "target_function"})

        self.assertTrue(result["success"])
        self.assertEqual(len(result["data"]), 1)
        match = result["data"][0]
        self.assertEqual(match["file"], "src\\example.py" if os.name == "nt" else "src/example.py")
        self.assertEqual(match["line"], 5)
        self.assertIn("def target_function():", match["snippet"])
        command = mock_run.call_args.kwargs["args"] if "args" in mock_run.call_args.kwargs else mock_run.call_args.args[0]
        self.assertIn("--fixed-strings", command)
        self.assertNotIn("--ignore-case", command)
        self.assertEqual(command[-2], "target_function")

    @patch("main.shutil.which", return_value="rg")
    @patch("main.subprocess.run")
    def test_search_can_ignore_case(self, mock_run, _: object) -> None:
        self.write_file("src/example.py", "ImportantValue = 1\n")
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=self.build_match_event(
                "src/example.py",
                1,
                "ImportantValue = 1",
            ),
            stderr="",
        )

        result = self.run_tool({"query": "importantvalue", "case_sensitive": False})

        self.assertTrue(result["success"])
        self.assertEqual(len(result["data"]), 1)
        self.assertIn("ImportantValue = 1", result["data"][0]["snippet"])
        command = mock_run.call_args.kwargs["args"] if "args" in mock_run.call_args.kwargs else mock_run.call_args.args[0]
        self.assertIn("--ignore-case", command)

    @patch("main.shutil.which", return_value="rg")
    @patch("main.subprocess.run")
    def test_search_respects_glob_filter(self, mock_run, _: object) -> None:
        self.write_file("src/example.py", "needle = 1\n")
        self.write_file("docs/example.md", "needle\n")
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=self.build_match_event("src/example.py", 1, "needle = 1"),
            stderr="",
        )

        result = self.run_tool({"query": "needle", "glob": "*.py"})

        self.assertTrue(result["success"])
        self.assertEqual(len(result["data"]), 1)
        self.assertTrue(result["data"][0]["file"].endswith("example.py"))
        command = mock_run.call_args.kwargs["args"] if "args" in mock_run.call_args.kwargs else mock_run.call_args.args[0]
        self.assertIn("--glob", command)
        self.assertIn("*.py", command)

    def test_search_fails_for_missing_path(self) -> None:
        result = self.run_tool({"query": "needle", "path": "missing"})

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "path not found")

    def test_search_fails_for_invalid_max_results(self) -> None:
        result = self.run_tool({"query": "needle", "max_results": 0})

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "max_results must be >= 1")

    @patch("main.shutil.which", return_value="rg")
    @patch("main.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="rg", timeout=20))
    def test_search_handles_timeout(self, _: object, __: object) -> None:
        self.write_file("src/example.py", "needle = 1\n")

        result = self.run_tool({"query": "needle"})

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "search command timed out")

    @patch("main.shutil.which", return_value=None)
    @patch("main.subprocess.run")
    def test_search_falls_back_to_python_without_rg(self, mock_run, _: object) -> None:
        self.write_file("src/example.py", "alpha\nbeta target\ngamma\n")
        self.write_file("docs/example.md", "target in docs\n")

        result = self.run_tool({"query": "target", "glob": "*.py"})

        self.assertTrue(result["success"])
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(result["data"][0]["line"], 2)
        self.assertIn("beta target", result["data"][0]["snippet"])
        mock_run.assert_not_called()

    @patch("main.shutil.which", return_value=None)
    def test_python_fallback_supports_ignore_case_and_regex(self, _: object) -> None:
        self.write_file("src/example.py", "UserID = 42\nnext_line\n")

        ignore_case_result = self.run_tool(
            {"query": "userid", "case_sensitive": False}
        )
        regex_result = self.run_tool(
            {"query": r"UserID\s*=\s*\d+", "regex": True}
        )

        self.assertTrue(ignore_case_result["success"])
        self.assertEqual(len(ignore_case_result["data"]), 1)
        self.assertTrue(regex_result["success"])
        self.assertEqual(len(regex_result["data"]), 1)


if __name__ == "__main__":
    unittest.main()
