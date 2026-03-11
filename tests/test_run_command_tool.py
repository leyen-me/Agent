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
        self.background_jobs_path = self.workspace / "background_jobs.json"
        self.background_logs_dir = self.workspace / "background_logs"
        self.background_job_store = agent_main.BackgroundJobStore(
            storage_path=self.background_jobs_path,
            log_dir=self.background_logs_dir,
        )
        self.tool = agent_main.RunCommandTool(
            background_job_store=self.background_job_store
        )

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

    @patch("main.subprocess.Popen")
    def test_run_command_supports_background_mode(self, mock_popen) -> None:
        mock_popen.return_value.pid = 4321

        result = self.run_tool(
            {
                "command": "npm run dev -- --host 0.0.0.0 --port 5173",
                "background": True,
            }
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["background"])
        self.assertEqual(result["data"]["pid"], 4321)
        self.assertEqual(result["data"]["pid_role"], "launcher")
        self.assertTrue(result["data"]["job_id"])
        self.assertTrue(self.background_jobs_path.exists())
        job = self.background_job_store.get(result["data"]["job_id"])
        self.assertIsNotNone(job)
        self.assertEqual(job.pid, 4321)
        self.assertEqual(job.status, "running")
        self.assertTrue(Path(job.stdout_log).exists())
        self.assertTrue(Path(job.stderr_log).exists())
        kwargs = mock_popen.call_args.kwargs
        self.assertTrue(kwargs["shell"])
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["cwd"], self.workspace)
        self.assertEqual(kwargs["env"]["CI"], "1")
        self.assertEqual(kwargs["env"]["TERM"], "dumb")

    @patch("main.subprocess.Popen")
    def test_run_command_treats_trailing_ampersand_as_background(self, mock_popen) -> None:
        mock_popen.return_value.pid = 9876

        result = self.run_tool({"command": "npm run dev -- --port 5173 &"})

        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["background"])
        self.assertEqual(result["data"]["pid"], 9876)
        self.assertEqual(mock_popen.call_args.args[0], "npm run dev -- --port 5173")

    def test_read_background_job_log_tool_reads_recent_lines(self) -> None:
        job = self.background_job_store.create_job(
            command="npm run dev",
            pid=1234,
            pid_role="launcher",
            cwd=self.workspace,
            stdout_log=self.background_logs_dir / "job.stdout.log",
            stderr_log=self.background_logs_dir / "job.stderr.log",
        )
        Path(job["stdout_log"]).write_text("a\nb\nc\n", encoding="utf-8")
        Path(job["stderr_log"]).write_text("err1\nerr2\n", encoding="utf-8")

        tool = agent_main.ReadBackgroundJobLogTool(self.background_job_store)
        result = json.loads(
            tool.run({"job_id": job["id"], "stream": "both", "tail_lines": 2})
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["stdout"], "b\nc")
        self.assertEqual(result["data"]["stderr"], "err1\nerr2")

    @patch("main.is_process_running", return_value=True)
    @patch("main.stop_background_process", return_value=(True, "stopped"))
    def test_stop_background_job_marks_job_stopped(
        self,
        mock_stop_background_process,
        mock_is_process_running,
    ) -> None:
        job = self.background_job_store.create_job(
            command="npm run dev",
            pid=2468,
            pid_role="launcher",
            cwd=self.workspace,
            stdout_log=self.background_logs_dir / "stop.stdout.log",
            stderr_log=self.background_logs_dir / "stop.stderr.log",
        )

        tool = agent_main.StopBackgroundJobTool(self.background_job_store)
        result = json.loads(tool.run({"job_id": job["id"]}))

        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["stopped"])
        self.assertEqual(result["data"]["status"], "stopped")
        refreshed = self.background_job_store.get(job["id"])
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.status, "stopped")
        mock_is_process_running.assert_called_once_with(2468)
        mock_stop_background_process.assert_called_once_with(2468)

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
