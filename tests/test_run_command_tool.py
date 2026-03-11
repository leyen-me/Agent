import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "https://example.com/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")

import main as agent_main


class RunCommandToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self._temp_dir.name).resolve()
        self.original_workspace = agent_main.WORKSPACE_DIR
        agent_main.WORKSPACE_DIR = self.workspace
        self.tool = agent_main.RunCommandTool()

    def tearDown(self) -> None:
        agent_main.WORKSPACE_DIR = self.original_workspace
        self._temp_dir.cleanup()

    def run_tool(self, parameters):
        return json.loads(self.tool.run(parameters))

    @patch("main.subprocess.run")
    def test_run_command_uses_non_interactive_settings(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["echo", "ok"],
            returncode=0,
            stdout=b"ok\n",
            stderr=b"",
        )

        result = self.run_tool({"command": "echo ok", "timeout": 5})

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["stdout"], "ok\n")
        kwargs = mock_run.call_args.kwargs
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["cwd"], self.workspace)
        self.assertEqual(kwargs["env"]["CI"], "1")
        self.assertEqual(kwargs["env"]["TERM"], "dumb")
        self.assertFalse(kwargs["text"])

    @patch("main.subprocess.run")
    def test_run_command_decodes_utf8_bytes_output(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["npm", "create"],
            returncode=0,
            stdout="│  ○ Yes\n".encode("utf-8"),
            stderr=b"",
        )

        result = self.run_tool({"command": "npm create vite@latest demo", "timeout": 5})

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["stdout"], "│  ○ Yes\n")

    @patch("main.subprocess.run")
    def test_run_command_reports_interactive_timeout_hint(self, mock_run) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd="npm create vite@latest demo -- --template vue",
            timeout=30,
            output="◆  Use Vite 8 beta (Experimental)?\n│  ○ Yes\n│  ● No\n",
            stderr="",
        )

        result = self.run_tool(
            {
                "command": "npm create vite@latest demo -- --template vue",
                "timeout": 30,
            }
        )

        self.assertFalse(result["success"])
        self.assertIn("interactive input", result["error"])
        self.assertIn("--no-interactive", result["error"])

    @patch("main.subprocess.run")
    def test_run_command_reports_generic_timeout_without_prompt_signal(self, mock_run) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd="python slow.py",
            timeout=12,
            output="still working",
            stderr="",
        )

        result = self.run_tool({"command": "python slow.py", "timeout": 12})

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "command timed out after 12s")


if __name__ == "__main__":
    unittest.main()
