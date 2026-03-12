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
            request_summary="为 session-a 创建项目结构",
        )
        duplicate_same_session = self.task_store.create_tasks(
            [{"description": "创建项目结构"}],
            session_id="session-a",
            request_summary="为 session-a 创建项目结构",
        )
        duplicate_other_session = self.task_store.create_tasks(
            [{"description": "创建项目结构"}],
            session_id="session-b",
            request_summary="为 session-b 创建项目结构",
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
            request_summary="处理旧会话任务",
        )
        tasks_b = self.task_store.create_tasks(
            [{"description": "当前会话任务"}],
            session_id="session-b",
            request_summary="处理当前会话任务",
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

        result = json.loads(
            tool.run(
                {
                    "request_summary": "生成项目脚手架",
                    "tasks": [{"description": "生成脚手架"}],
                }
            )
        )

        self.assertTrue(result["success"])
        created = self.task_store.list_tasks("session-plan")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["description"], "生成脚手架")
        self.assertEqual(created[0]["request_summary"], "生成项目脚手架")

    def test_task_plan_tool_rejects_new_request_when_active_request_exists(self) -> None:
        self.task_store.create_tasks(
            [{"description": "先执行已有任务"}],
            session_id="session-plan",
            request_summary="已有未完成请求",
        )
        tool = agent_main.TaskPlanTool(
            self.task_store,
            session_id_provider=lambda: "session-plan",
        )

        result = json.loads(
            tool.run(
                {
                    "request_summary": "另一个请求",
                    "tasks": [{"description": "不应创建的新任务"}],
                }
            )
        )

        self.assertFalse(result["success"])
        self.assertEqual(
            result["error"],
            "active request exists; continue executing current request before creating a new task plan",
        )
        self.assertEqual(len(self.task_store.list_requests("session-plan")), 1)

    def test_read_tasks_tool_returns_only_current_session_tasks(self) -> None:
        self.task_store.create_tasks(
            [{"description": "会话A任务"}],
            session_id="session-a",
            request_summary="处理会话A",
        )
        self.task_store.create_tasks(
            [{"description": "会话B任务"}],
            session_id="session-b",
            request_summary="处理会话B",
        )
        tool = agent_main.ReadTasksTool(
            self.task_store,
            session_id_provider=lambda: "session-b",
        )

        result = json.loads(tool.run({}))

        self.assertTrue(result["success"])
        self.assertEqual(
            [request["summary"] for request in result["data"]],
            ["处理会话B"],
        )
        self.assertEqual(result["data"][0]["tasks"][0]["description"], "会话B任务")

    def test_read_tasks_tool_rejects_other_session_task_id(self) -> None:
        tasks_a = self.task_store.create_tasks(
            [{"description": "会话A任务"}],
            session_id="session-a",
            request_summary="处理会话A",
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

    def test_storage_uses_request_centered_shape(self) -> None:
        self.task_store.create_tasks(
            [{"description": "初始化项目"}],
            session_id="session-a",
            request_summary="初始化一个新项目",
            user_input="帮我初始化一个项目",
        )

        payload = json.loads(self.storage_path.read_text(encoding="utf-8"))

        self.assertIn("requests", payload)
        self.assertEqual(len(payload["requests"]), 1)
        request = payload["requests"][0]
        self.assertEqual(request["summary"], "初始化一个新项目")
        self.assertEqual(request["user_input"], "帮我初始化一个项目")
        self.assertEqual(request["tasks"][0]["description"], "初始化项目")


if __name__ == "__main__":
    unittest.main()
