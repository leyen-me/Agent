"""纯对话脚本：与 main 共用 .agent/config.json，无工具、无任务编排。

交互体验（提示符、流式输出、【思考】/【回答】着色）与 BaseAgent 终端表现一致。
"""

from __future__ import annotations

import json
import logging
import os
import platform
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI

# ==== 与 main 对齐的最小运行时配置 ====

SCRIPT_DIR = Path(__file__).resolve().parent
_AGENT_DIR = SCRIPT_DIR / ".agent"
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_CONFIG_FILE = _AGENT_DIR / "config.json"
_LOG_FILE = _AGENT_DIR / "agent.log"
_DEFAULT_CONFIG = {
    "OPENAI_API_KEY": None,
    "OPENAI_BASE_URL": None,
    "OPENAI_MODEL": None,
    "WORKSPACE_DIR": None,
    "OPENAI_ENABLE_THINKING": True,
}


def _mark_hidden_on_windows(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        hidden_flag = 0x2
        current_attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if current_attrs == 0xFFFFFFFF:
            return
        ctypes.windll.kernel32.SetFileAttributesW(str(path), current_attrs | hidden_flag)
    except Exception:
        return


def _ensure_runtime_storage() -> None:
    _AGENT_DIR.mkdir(parents=True, exist_ok=True)
    _mark_hidden_on_windows(_AGENT_DIR)
    if not _CONFIG_FILE.exists():
        _CONFIG_FILE.write_text(
            json.dumps(_DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    _LOG_FILE.touch(exist_ok=True)


_ensure_runtime_storage()


def _load_runtime_config() -> Dict[str, Any]:
    try:
        content = _CONFIG_FILE.read_text(encoding="utf-8").strip()
    except Exception as exc:
        raise ValueError(f"读取配置文件失败：{_CONFIG_FILE}") from exc
    if not content:
        return {}
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"配置文件 JSON 格式无效：{_CONFIG_FILE}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"配置文件顶层必须是对象：{_CONFIG_FILE}")
    return data


RUNTIME_CONFIG = _load_runtime_config()


def get_config_value(
    key: str,
    *,
    default: Optional[str] = None,
    required: bool = False,
) -> str:
    config_value = RUNTIME_CONFIG.get(key)
    if config_value is not None:
        value = str(config_value).strip()
        if value:
            return value
    env_value = os.getenv(key)
    if env_value is not None:
        value = env_value.strip()
        if value:
            return value
    if default is not None:
        return default
    if required:
        raise ValueError(
            f"缺少必填配置 {key}，请先在 {_CONFIG_FILE} 中设置，或提供同名环境变量。"
        )
    return ""


def get_optional_bool_config(*keys: str) -> Optional[bool]:
    truthy = {"1", "true", "yes", "on"}
    falsy = {"0", "false", "no", "off"}
    for key in keys:
        config_value = RUNTIME_CONFIG.get(key)
        raw_value = config_value if config_value is not None else os.getenv(key)
        if raw_value in (None, ""):
            continue
        if isinstance(raw_value, bool):
            return raw_value
        normalized = str(raw_value).strip().lower()
        if normalized in truthy:
            return True
        if normalized in falsy:
            return False
    return None


logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    handlers=[logging.FileHandler(_LOG_FILE, encoding="utf-8", mode="a")],
)
logger = logging.getLogger("simple-chat")

OPENAI_API_KEY = get_config_value("OPENAI_API_KEY", required=True)
OPENAI_BASE_URL = get_config_value("OPENAI_BASE_URL", required=True)
OPENAI_MODEL = get_config_value("OPENAI_MODEL", required=True)
DEFAULT_WORKSPACE_DIR = Path.cwd().resolve()
WORKSPACE_DIR = Path(
    get_config_value("WORKSPACE_DIR", default=str(DEFAULT_WORKSPACE_DIR))
).expanduser().resolve()
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

OPENAI_ENABLE_THINKING = get_optional_bool_config("OPENAI_ENABLE_THINKING")
if OPENAI_ENABLE_THINKING is None:
    OPENAI_ENABLE_THINKING = True

ENABLE_COLOR = os.getenv("NO_COLOR") is None and os.getenv("TERM") != "dumb"
ANSI_RESET = "\033[0m"
PLAN_COLOR = "\033[38;5;25m"
EXECUTE_COLOR = "\033[38;5;81m"
INFO_COLOR = "\033[38;5;244m"
REASONING_COLOR = "\033[38;5;242m"


def color_text(text: str, color: str) -> str:
    if not ENABLE_COLOR:
        return text
    return f"{color}{text}{ANSI_RESET}"


def get_display_width(text: str) -> int:
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def pad_to_display_width(text: str, target_width: int) -> str:
    padding = max(target_width - get_display_width(text), 0)
    return text + (" " * padding)


def print_info_table(rows: List[List[str]]) -> None:
    normalized_rows = [[str(cell) for cell in row] for row in rows]
    left_width = max(get_display_width(row[0]) for row in normalized_rows)
    right_width = max(get_display_width(row[1]) for row in normalized_rows)
    border = f"+-{'-' * left_width}-+-{'-' * right_width}-"
    print(color_text(border, PLAN_COLOR))
    for left, right in normalized_rows:
        print(
            color_text(f"| {pad_to_display_width(left, left_width)} |", PLAN_COLOR)
            + " "
            + color_text(pad_to_display_width(right, right_width), EXECUTE_COLOR)
        )
    print(color_text(border, PLAN_COLOR))


def print_console_block(title: str, lines: List[str], color: str = INFO_COLOR) -> None:
    normalized_lines = [str(line) for line in lines]
    title_text = f"[{title}]"
    content_width = max(
        [get_display_width(title_text), *(get_display_width(line) for line in normalized_lines)]
    )
    border = color_text("=" * content_width, color)
    print()
    print(border)
    print(color_text(title_text, color))
    for line in normalized_lines:
        print(line)
    print(border)


def get_system_name() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macOS"
    if system == "windows":
        return "Windows"
    if system == "linux":
        return "Linux"
    return platform.system() or "Unknown"


DEFAULT_CONTEXT_WINDOW = 200000

# 通用对话官方推荐参数（按思考模式区分）
# Thinking: temp=1.0, top_p=0.95, top_k=20, min_p=0, presence_penalty=1.5
# Non-thinking: temp=0.7, top_p=0.8, top_k=20, min_p=0, presence_penalty=1.5
_PARAMS_THINKING = (1.0, 0.95, 20, 0, 1.5)
_PARAMS_NON_THINKING = (0.7, 0.8, 20, 0, 1.5)

MODEL_CONTEXT_WINDOWS = {
    "minimax-m2.5": 204800,
    "minimax-m2.5-highspeed": 204800,
}


def get_optional_int_config(*keys: str) -> Optional[int]:
    for key in keys:
        config_value = RUNTIME_CONFIG.get(key)
        raw_value = config_value if config_value is not None else os.getenv(key)
        if raw_value in (None, ""):
            continue
        try:
            value = int(str(raw_value).strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def resolve_model_context_window(model_name: str) -> Optional[int]:
    configured = get_optional_int_config("OPENAI_CONTEXT_WINDOW", "MODEL_CONTEXT_WINDOW")
    if configured is not None:
        return configured
    return MODEL_CONTEXT_WINDOWS.get(model_name.strip().lower(), DEFAULT_CONTEXT_WINDOW)


def format_percent(numerator: int, denominator: Optional[int]) -> str:
    if denominator is None or denominator <= 0:
        return "未知"
    percent = (numerator / denominator) * 100
    return f"{percent:.1f}%"


def build_progress_bar(numerator: int, denominator: Optional[int], width: int = 20) -> str:
    if denominator is None or denominator <= 0:
        return "[????????????????????]"
    ratio = max(0.0, min(numerator / denominator, 1.0))
    filled = int(round(ratio * width))
    return f"[{'#' * filled}{'-' * (width - filled)}]"


def format_timestamp(timestamp: Any) -> str:
    if not isinstance(timestamp, (int, float)):
        return "未知"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


@dataclass
class UsageSnapshot:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    updated_at: float = field(default_factory=time.time)
    raw: Dict[str, Any] = field(default_factory=dict)


class SimpleChatAgent:
    """与 BaseAgent 终端流式表现一致，但不注册、不调用任何工具。"""

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        agent_name: str = "助手",
    ) -> None:
        self.model = model or OPENAI_MODEL
        self.agent_name = agent_name
        self.agent_color = INFO_COLOR
        self.client = OpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL,
            timeout=300.0,
        )
        self.system_prompt = system_prompt or "You are a helpful assistant."
        self.base_messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
        ]
        self.messages: List[Dict[str, Any]] = list(self.base_messages)
        self.latest_usage: Optional[UsageSnapshot] = None

    def _usage_to_dict(self, usage: Any) -> Dict[str, Any]:
        if usage is None:
            return {}
        if isinstance(usage, dict):
            return json.loads(json.dumps(usage, ensure_ascii=False))
        for attr in ("model_dump", "dict"):
            method = getattr(usage, attr, None)
            if callable(method):
                try:
                    data = method()
                except TypeError:
                    continue
                if isinstance(data, dict):
                    return json.loads(json.dumps(data, ensure_ascii=False))
        return {
            key: value
            for key, value in vars(usage).items()
            if not key.startswith("_") and not callable(value)
        }

    def _int_from_usage(self, raw_usage: Dict[str, Any], key: str) -> int:
        value = raw_usage.get(key, 0)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def update_usage_snapshot(self, usage: Any) -> None:
        raw_usage = self._usage_to_dict(usage)
        if not raw_usage:
            return
        self.latest_usage = UsageSnapshot(
            prompt_tokens=self._int_from_usage(raw_usage, "prompt_tokens"),
            completion_tokens=self._int_from_usage(raw_usage, "completion_tokens"),
            total_tokens=self._int_from_usage(raw_usage, "total_tokens"),
            updated_at=time.time(),
            raw=raw_usage,
        )

    def _coerce_stream_text(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, list):
            return ""
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
            else:
                text = getattr(item, "text", None) or getattr(item, "content", None)
            if isinstance(text, str) and text:
                parts.append(text)
        return "".join(parts)

    def get_reasoning_delta_text(self, delta: Any) -> str:
        for attr in ("reasoning_content", "reasoning"):
            text = self._coerce_stream_text(getattr(delta, attr, None))
            if text:
                return text
        return ""

    def get_context_window(self) -> Optional[int]:
        return resolve_model_context_window(self.model)

    def get_usage_report_lines(self) -> List[str]:
        usage = self.latest_usage
        if usage is None:
            return ["当前还没有 usage 数据，请先完成至少一轮对话。"]
        context_limit = self.get_context_window()
        context_percent = format_percent(usage.prompt_tokens, context_limit)
        total_percent = format_percent(usage.total_tokens, context_limit)
        return [
            f"模型：{self.model}",
            f"当前上下文：{usage.prompt_tokens} tokens",
            (
                "上下文占用："
                f"{usage.prompt_tokens} / {context_limit if context_limit else '未知'} "
                f"({context_percent}) {build_progress_bar(usage.prompt_tokens, context_limit)}"
            ),
            f"本轮输出：{usage.completion_tokens} tokens",
            f"当前总计：{usage.total_tokens} tokens",
            (
                "总占用："
                f"{usage.total_tokens} / {context_limit if context_limit else '未知'} "
                f"({total_percent}) {build_progress_bar(usage.total_tokens, context_limit)}"
            ),
            f"更新时间：{format_timestamp(usage.updated_at)}",
        ]

    def reset_conversation(self) -> None:
        self.messages = list(self.base_messages)
        self.latest_usage = None

    def chat(self, message: str, *, reset_history: bool = False) -> str:
        if reset_history:
            self.reset_conversation()
        self.messages.append({"role": "user", "content": message})
        temp, top_p, top_k, min_p, presence_penalty = (
            _PARAMS_NON_THINKING if not OPENAI_ENABLE_THINKING else _PARAMS_THINKING
        )
        api_kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
            "stream": True,
            "temperature": temp,
            "top_p": top_p,
            "presence_penalty": presence_penalty,
            "stream_options": {"include_usage": True, "continuous_usage_stats": True},
        }
        extra_body: Dict[str, Any] = {"top_k": top_k, "min_p": min_p}
        if not OPENAI_ENABLE_THINKING:
            extra_body["chat_template_kwargs"] = {"enable_thinking": False}
        api_kwargs["extra_body"] = extra_body

        stream = self.client.chat.completions.create(**api_kwargs)

        content_parts: List[str] = []
        print(f"\n{color_text(f'{self.agent_name}：', self.agent_color)}", end="", flush=True)
        reasoning_started = False
        answer_started = False
        for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                self.update_usage_snapshot(usage)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            logger.info(delta)

            reasoning_text = self.get_reasoning_delta_text(delta)
            if reasoning_text:
                if not reasoning_started:
                    print(
                        "\n" + color_text("【思考】", REASONING_COLOR) + " ",
                        end="",
                        flush=True,
                    )
                    reasoning_started = True
                print(color_text(reasoning_text, REASONING_COLOR), end="", flush=True)

            if hasattr(delta, "content") and delta.content:
                content_parts.append(delta.content)
                if reasoning_started and not answer_started:
                    print(
                        "\n" + color_text("【回答】", self.agent_color) + " ",
                        end="",
                        flush=True,
                    )
                    answer_started = True
                print(delta.content, end="", flush=True)

        full_content = "".join(content_parts)
        if full_content:
            self.messages.append({"role": "assistant", "content": full_content})
            print()
            return full_content
        logger.warning("API 返回空响应")
        return ""


def handle_slash(agent: SimpleChatAgent, text: str) -> Optional[bool]:
    """处理斜杠命令。返回 True 表示已消费输入，False 表示退出程序，None 表示非命令。"""
    if not text.startswith("/"):
        return None
    parts = text.split(maxsplit=1)
    name = parts[0].lower()
    if name in ("/exit", "/quit"):
        return False
    if name == "/help":
        print_console_block(
            "可用命令",
            [
                "/help：显示本帮助",
                "/reset：清空当前对话上下文",
                "/usage：显示最近一次 usage",
                "/exit 或 /quit：退出",
            ],
            PLAN_COLOR,
        )
        return True
    if name == "/reset":
        agent.reset_conversation()
        print_console_block("会话", ["已清空当前对话上下文。"], PLAN_COLOR)
        return True
    if name == "/usage":
        print_console_block("Usage", agent.get_usage_report_lines(), PLAN_COLOR)
        return True
    print_console_block(
        "命令提示",
        [f"未知命令：{parts[0]}", "输入 /help 查看可用命令"],
        PLAN_COLOR,
    )
    return True


def main() -> None:
    agent = SimpleChatAgent()
    print_info_table(
        [
            ["欢迎语", "简易对话（无工具），与模型直连聊天"],
            ["当前系统", get_system_name()],
            ["当前工作区", str(WORKSPACE_DIR)],
            ["配置文件", str(_CONFIG_FILE)],
            ["命令帮助", "输入 /help 查看可用命令"],
        ]
    )
    try:
        while True:
            user_input = input("\n用户：")
            stripped = user_input.strip()
            cmd = handle_slash(agent, stripped)
            if cmd is not None:
                if not cmd:
                    break
                continue
            if not stripped:
                continue
            agent.chat(user_input)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
