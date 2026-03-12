import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "https://example.com/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")

import main as agent_main


def make_chunk(delta):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta)],
        usage=None,
    )


class AgentStreamingReasoningTest(unittest.TestCase):
    @patch("main.OpenAI")
    def test_chat_prints_reasoning_content_without_saving_to_context(
        self, mock_openai
    ) -> None:
        mock_openai.return_value.chat.completions.create.return_value = [
            make_chunk(
                SimpleNamespace(
                    content=None,
                    reasoning_content="先分析一下问题。",
                    reasoning=None,
                    tool_calls=None,
                )
            ),
            make_chunk(
                SimpleNamespace(
                    content="这是最终答案。",
                    reasoning_content=None,
                    reasoning=None,
                    tool_calls=None,
                )
            ),
        ]
        agent = agent_main.BaseAgent(system_prompt="test", agent_name="测试助手")
        output = io.StringIO()

        with patch("sys.stdout", output), patch.object(
            agent_main, "ENABLE_COLOR", False
        ):
            result = agent.chat("你好")

        self.assertEqual(result, "这是最终答案。")
        rendered = output.getvalue()
        self.assertIn("测试助手：\n【思考】 先分析一下问题。\n【回答】 这是最终答案。", rendered)
        self.assertIn("这是最终答案。", rendered)
        self.assertEqual(
            agent.messages[-1],
            {"role": "assistant", "content": "这是最终答案。"},
        )
        self.assertNotIn("先分析一下问题。", agent.messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
