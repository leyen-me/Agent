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
        self.task_store = agent_main.TaskStore(storage_path=self.workspace / "task.json")
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

    def test_read_file_lines_rejects_agent_internal_path(self) -> None:
        internal_dir = self.workspace / ".agent"
        internal_dir.mkdir(parents=True, exist_ok=True)
        (internal_dir / "secret.log").write_text("hidden\n", encoding="utf-8")

        with patch.object(agent_main, "_AGENT_DIR", internal_dir):
            tool = agent_main.ReadFileLinesTool()
            result = json.loads(tool.run({"path": ".agent/secret.log"}))

        self.assertFalse(result["success"])
        self.assertIn(".agent", result["error"])

    def test_list_files_skips_agent_internal_directory(self) -> None:
        (self.workspace / ".agent").mkdir(parents=True, exist_ok=True)
        (self.workspace / ".agent" / "secret.log").write_text("hidden\n", encoding="utf-8")
        (self.workspace / "visible.txt").write_text("ok\n", encoding="utf-8")

        tool = agent_main.ListFilesTool()
        result = json.loads(tool.run({"path": ".", "depth": 2}))

        self.assertTrue(result["success"])
        paths = {item["path"] for item in result["data"]}
        self.assertIn("visible.txt", paths)
        self.assertNotIn(".agent", paths)
        self.assertNotIn(".agent\\secret.log", paths)

    @patch("main.time.sleep")
    def test_sleep_tool_waits_without_shell_command(self, mock_sleep) -> None:
        tool = agent_main.SleepTool()

        result = json.loads(tool.run({"seconds": 2}))

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["slept_seconds"], 2.0)
        mock_sleep.assert_called_once_with(2.0)

    @patch("main.wait_for_background_service")
    @patch("main.launch_background_command")
    def test_start_background_service_returns_ready_result(
        self,
        mock_launch_background_command,
        mock_wait_for_background_service,
    ) -> None:
        mock_launch_background_command.return_value = {
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "command": "npm run dev",
            "background": True,
            "pid": 7256,
            "pid_role": "launcher",
            "job_id": "svc12345",
            "status": "running",
            "stdout_log": str(self.background_logs_dir / "svc12345.stdout.log"),
            "stderr_log": str(self.background_logs_dir / "svc12345.stderr.log"),
        }
        mock_wait_for_background_service.return_value = {
            "id": "svc12345",
            "command": "npm run dev",
            "pid": 7256,
            "pid_role": "launcher",
            "status": "running",
            "stdout_log": str(self.background_logs_dir / "svc12345.stdout.log"),
            "stderr_log": str(self.background_logs_dir / "svc12345.stderr.log"),
            "ready": True,
            "timed_out": False,
            "attempts": 2,
            "verification": "tcp_port",
            "host": "localhost",
            "port": 5173,
            "url": "http://localhost:5173",
            "stdout": "ready",
            "stderr": "",
        }

        tool = agent_main.StartBackgroundServiceTool(self.background_job_store)
        result = json.loads(
            tool.run(
                {
                    "command": "npm run dev",
                    "port": 5173,
                    "startup_timeout": 10,
                    "poll_interval": 1,
                }
            )
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["ready"])
        self.assertEqual(result["data"]["verification"], "tcp_port")
        self.assertEqual(result["data"]["url"], "http://localhost:5173")
        mock_launch_background_command.assert_called_once()
        mock_wait_for_background_service.assert_called_once()

    @patch("main.wait_for_background_service")
    @patch("main.launch_background_command")
    def test_start_background_service_returns_bounded_timeout_result(
        self,
        mock_launch_background_command,
        mock_wait_for_background_service,
    ) -> None:
        mock_launch_background_command.return_value = {
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "command": "npm run dev",
            "background": True,
            "pid": 8001,
            "pid_role": "launcher",
            "job_id": "svc99999",
            "status": "running",
            "stdout_log": str(self.background_logs_dir / "svc99999.stdout.log"),
            "stderr_log": str(self.background_logs_dir / "svc99999.stderr.log"),
        }
        mock_wait_for_background_service.return_value = {
            "id": "svc99999",
            "command": "npm run dev",
            "pid": 8001,
            "pid_role": "launcher",
            "status": "running",
            "stdout_log": str(self.background_logs_dir / "svc99999.stdout.log"),
            "stderr_log": str(self.background_logs_dir / "svc99999.stderr.log"),
            "ready": False,
            "timed_out": True,
            "attempts": 3,
            "verification": "timeout",
            "host": "localhost",
            "port": 5173,
            "stdout": "",
            "stderr": "",
        }

        tool = agent_main.StartBackgroundServiceTool(self.background_job_store)
        result = json.loads(tool.run({"command": "npm run dev", "port": 5173}))

        self.assertTrue(result["success"])
        self.assertFalse(result["data"]["ready"])
        self.assertTrue(result["data"]["timed_out"])
        self.assertEqual(result["data"]["verification"], "timeout")

    @patch("main.wait_for_background_service")
    @patch("main.launch_background_command")
    def test_execute_agent_records_background_job_from_start_service_tool(
        self,
        mock_launch_background_command,
        mock_wait_for_background_service,
    ) -> None:
        mock_launch_background_command.return_value = {
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "command": "npm run dev",
            "background": True,
            "pid": 9123,
            "pid_role": "launcher",
            "job_id": "svc77777",
            "status": "running",
            "stdout_log": str(self.background_logs_dir / "svc77777.stdout.log"),
            "stderr_log": str(self.background_logs_dir / "svc77777.stderr.log"),
        }
        mock_wait_for_background_service.return_value = {
            "id": "svc77777",
            "command": "npm run dev",
            "pid": 9123,
            "pid_role": "launcher",
            "status": "running",
            "stdout_log": str(self.background_logs_dir / "svc77777.stdout.log"),
            "stderr_log": str(self.background_logs_dir / "svc77777.stderr.log"),
            "ready": True,
            "timed_out": False,
            "attempts": 1,
            "verification": "tcp_port",
            "host": "localhost",
            "port": 5173,
            "url": "http://localhost:5173",
            "stdout": "",
            "stderr": "",
        }

        exec_agent = agent_main.ExecuteAgent(
            self.task_store,
            background_job_store=self.background_job_store,
        )
        result = exec_agent.execute_tool(
            "start_background_service",
            json.dumps({"command": "npm run dev", "port": 5173}),
        )

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(len(exec_agent.recent_background_jobs), 1)
        self.assertEqual(exec_agent.recent_background_jobs[0]["id"], "svc77777")

    def test_task_update_result_auto_appends_background_job_summary(self) -> None:
        task = self.task_store.create_tasks(["启动开发服务"])[0]
        exec_agent = agent_main.ExecuteAgent(
            self.task_store,
            background_job_store=self.background_job_store,
        )
        exec_agent.active_task_id = task["id"]
        exec_agent.recent_background_jobs = [
            {
                "id": "job12345",
                "pid": 4321,
                "pid_role": "launcher",
                "status": "running",
                "stdout_log": str(self.background_logs_dir / "job12345.stdout.log"),
                "stderr_log": str(self.background_logs_dir / "job12345.stderr.log"),
                "command": "npm run dev -- --port 5173",
            }
        ]

        tool = agent_main.TaskUpdateTool(
            self.task_store,
            result_enricher=exec_agent.enrich_task_result_with_background_jobs,
        )
        result = json.loads(
            tool.run(
                {
                    "task_id": task["id"],
                    "status": "done",
                    "result": "开发服务已启动",
                }
            )
        )

        self.assertTrue(result["success"])
        enriched = result["data"]["result"]
        self.assertIn("开发服务已启动", enriched)
        self.assertIn("后台任务：", enriched)
        self.assertIn("job_id=job12345", enriched)
        self.assertIn("stdout=", enriched)

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
