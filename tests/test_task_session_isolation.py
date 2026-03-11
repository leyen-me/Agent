import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "https://example.com/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")

import main as agent_main


class TaskSessionIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.storage_path = Path(self._temp_dir.name) / "task.json"
        self.task_store = agent_main.TaskStore(self.storage_path)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_create_tasks_isolated_by_session(self) -> None:
        first = self.task_store.create_tasks(
            [{"description": "创建项目结构"}],
            session_id="session-a",
        )
        duplicate_same_session = self.task_store.create_tasks(
            [{"description": "创建项目结构"}],
            session_id="session-a",
        )
        duplicate_other_session = self.task_store.create_tasks(
            [{"description": "创建项目结构"}],
            session_id="session-b",
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(duplicate_same_session, [])
        self.assertEqual(len(duplicate_other_session), 1)
        self.assertEqual(
            [task["session_id"] for task in self.task_store.list_tasks("session-a")],
            ["session-a"],
        )
        self.assertEqual(
            [task["session_id"] for task in self.task_store.list_tasks("session-b")],
            ["session-b"],
        )

    def test_pending_and_completed_queries_respect_session(self) -> None:
        tasks_a = self.task_store.create_tasks(
            [{"description": "旧会话任务"}],
            session_id="session-a",
        )
        tasks_b = self.task_store.create_tasks(
            [{"description": "当前会话任务"}],
            session_id="session-b",
        )
        self.task_store.update_task(tasks_a[0]["id"], "failed", result="历史失败")

        next_b = self.task_store.get_next_pending(session_id="session-b")

        self.assertIsNotNone(next_b)
        self.assertEqual(next_b.description, "当前会话任务")
        self.assertEqual(
            [task["description"] for task in self.task_store.completed_tasks("session-a")],
            ["旧会话任务"],
        )
        self.assertEqual(self.task_store.completed_tasks("session-b"), [])
        self.assertTrue(self.task_store.has_active_tasks("session-b"))
        self.assertFalse(self.task_store.has_active_tasks("session-a"))

    def test_task_plan_tool_uses_current_session_id(self) -> None:
        tool = agent_main.TaskPlanTool(
            self.task_store,
            session_id_provider=lambda: "session-plan",
        )

        result = json.loads(tool.run({"tasks": [{"description": "生成脚手架"}]}))

        self.assertTrue(result["success"])
        created = self.task_store.list_tasks("session-plan")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["description"], "生成脚手架")

    def test_read_tasks_tool_returns_only_current_session_tasks(self) -> None:
        self.task_store.create_tasks(
            [{"description": "会话A任务"}],
            session_id="session-a",
        )
        self.task_store.create_tasks(
            [{"description": "会话B任务"}],
            session_id="session-b",
        )
        tool = agent_main.ReadTasksTool(
            self.task_store,
            session_id_provider=lambda: "session-b",
        )

        result = json.loads(tool.run({}))

        self.assertTrue(result["success"])
        self.assertEqual(
            [task["description"] for task in result["data"]],
            ["会话B任务"],
        )

    def test_read_tasks_tool_rejects_other_session_task_id(self) -> None:
        tasks_a = self.task_store.create_tasks(
            [{"description": "会话A任务"}],
            session_id="session-a",
        )
        tool = agent_main.ReadTasksTool(
            self.task_store,
            session_id_provider=lambda: "session-b",
        )

        result = json.loads(tool.run({"task_id": tasks_a[0]["id"]}))

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "task not found")

    @patch("main.execute_single_task", return_value={"executed": False, "task": None})
    def test_execute_next_task_tool_dispatches_current_session_only(self, mock_execute) -> None:
        tool = agent_main.ExecuteNextTaskTool(
            self.task_store,
            exec_agent=object(),
            session_id_provider=lambda: "session-exec",
        )

        result = json.loads(tool.run({}))

        self.assertTrue(result["success"])
        mock_execute.assert_called_once()
        self.assertEqual(mock_execute.call_args.kwargs["session_id"], "session-exec")


if __name__ == "__main__":
    unittest.main()
