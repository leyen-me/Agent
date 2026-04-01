import argparse
import ctypes
import fnmatch
import json
import locale
import logging
import os
import platform
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from html import escape
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from openai import OpenAI

try:
    import pathspec
except ImportError:  # pragma: no cover - 依赖未安装时退回内置忽略规则
    pathspec = None


# ==== 日志配置 ====

SCRIPT_DIR = Path(__file__).resolve().parent
_AGENT_DIR = SCRIPT_DIR / ".agent"
_TEMP_DIR = _AGENT_DIR / "tmp"
_LOG_ROOT_DIR = SCRIPT_DIR / ".logs"
_AGENT_LOG_DIR = _LOG_ROOT_DIR / "agent"
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_CONFIG_FILE = _AGENT_DIR / "config.json"
_HISTORY_FILE = _AGENT_DIR / "history.json"
_TASK_FILE = _AGENT_DIR / "task.json"
_BACKGROUND_JOBS_FILE = _AGENT_DIR / "background_jobs.json"
_BACKGROUND_LOG_DIR = _LOG_ROOT_DIR / "background"
_DEFAULT_CONFIG = {
    "OPENAI_API_KEY": None,
    "OPENAI_BASE_URL": None,
    "OPENAI_MODEL": None,
    "PLAN_MODEL": None,
    "EXEC_MODEL": None,
    "WORKSPACE_DIR": None,
    "OPENAI_ENABLE_THINKING": True,
}


def _mark_hidden_on_windows(path: Path) -> None:
    """在 Windows 上尽力给运行时目录设置隐藏属性。"""
    if os.name != "nt":
        return

    try:
        hidden_flag = 0x2
        current_attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if current_attrs == 0xFFFFFFFF:
            return
        ctypes.windll.kernel32.SetFileAttributesW(str(path), current_attrs | hidden_flag)
    except Exception:
        # 隐藏属性设置失败不影响主流程。
        return


def _ensure_runtime_storage() -> None:
    """确保运行时目录及文件存在。"""
    _AGENT_DIR.mkdir(parents=True, exist_ok=True)
    _TEMP_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    _AGENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    _BACKGROUND_LOG_DIR.mkdir(parents=True, exist_ok=True)
    _mark_hidden_on_windows(_AGENT_DIR)
    _mark_hidden_on_windows(_LOG_ROOT_DIR)
    if not _CONFIG_FILE.exists():
        _CONFIG_FILE.write_text(
            json.dumps(_DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if not _HISTORY_FILE.exists():
        _HISTORY_FILE.write_text('{"sessions": []}\n', encoding="utf-8")
    if not _TASK_FILE.exists():
        _TASK_FILE.write_text("[]\n", encoding="utf-8")
    if not _BACKGROUND_JOBS_FILE.exists():
        _BACKGROUND_JOBS_FILE.write_text("[]\n", encoding="utf-8")


_ensure_runtime_storage()


def _load_runtime_config() -> Dict[str, Any]:
    """加载 .agent/config.json，格式无效时抛出异常。"""
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


def get_current_agent_log_path() -> Path:
    """返回当天的 agent 日志文件路径，例如 .logs/agent/2026-04-01.log。"""
    return _AGENT_LOG_DIR / f"{time.strftime('%Y-%m-%d')}.log"


class DailyArchiveFileHandler(logging.Handler):
    """按天写入归档文件名风格的日志处理器。"""

    terminator = "\n"

    def __init__(self, log_dir: Path, encoding: str = "utf-8") -> None:
        super().__init__()
        self.log_dir = log_dir
        self.encoding = encoding
        self._current_path: Optional[Path] = None
        self._stream = None

    def _ensure_stream(self) -> None:
        target_path = get_current_agent_log_path()
        if self._stream is not None and self._current_path == target_path:
            return
        if self._stream is not None:
            self._stream.close()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._stream = target_path.open("a", encoding=self.encoding)
        self._current_path = target_path

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.acquire()
            self._ensure_stream()
            if self._stream is None:
                return
            self._stream.write(self.format(record) + self.terminator)
            self._stream.flush()
        except Exception:
            self.handleError(record)
        finally:
            self.release()

    def close(self) -> None:
        try:
            self.acquire()
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            self._current_path = None
        finally:
            self.release()
        super().close()


def get_config_value(
    key: str,
    *,
    default: Optional[str] = None,
    required: bool = False,
) -> str:
    """优先从配置文件读取，其次回退到环境变量。"""
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

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    handlers=[
        # logging.StreamHandler(), // 将日志输出到控制台
        DailyArchiveFileHandler(_AGENT_LOG_DIR),
    ],
)
logger = logging.getLogger("Agent")


# ==== 运行时配置 ====

OPENAI_API_KEY = get_config_value("OPENAI_API_KEY", required=True)
OPENAI_BASE_URL = get_config_value("OPENAI_BASE_URL", required=True)
OPENAI_MODEL = get_config_value("OPENAI_MODEL", required=True)
PLAN_MODEL = get_config_value("PLAN_MODEL", default=OPENAI_MODEL)
EXEC_MODEL = get_config_value("EXEC_MODEL", default=OPENAI_MODEL)
DEFAULT_WORKSPACE_DIR = Path.cwd().resolve()
WORKSPACE_DIR = Path(
    get_config_value("WORKSPACE_DIR", default=str(DEFAULT_WORKSPACE_DIR))
).expanduser().resolve()
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_INSTRUCTIONS_FILE = WORKSPACE_DIR / "AGENTS.md"


def detect_shell_name() -> str:
    """推断当前会话 shell 名称，供提示词了解命令环境。"""
    shell_path = os.getenv("SHELL") or os.getenv("COMSPEC") or ""
    if shell_path:
        return Path(shell_path).name
    if os.name == "nt":
        return "cmd.exe"
    return "unknown"


def detect_is_git_repo(path: Path) -> bool:
    """判断给定目录是否位于 git 仓库中。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


SHELL_NAME = detect_shell_name()
IS_GIT_REPO = detect_is_git_repo(WORKSPACE_DIR)


def load_project_instructions_text() -> str:
    """读取工作区中的 AGENTS.md，作为项目级用户要求。"""
    try:
        if not PROJECT_INSTRUCTIONS_FILE.exists():
            return ""
        return PROJECT_INSTRUCTIONS_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


PROJECT_INSTRUCTIONS_TEXT = load_project_instructions_text()

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
    """计算字符串在等宽终端中的显示宽度。"""
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def pad_to_display_width(text: str, target_width: int) -> str:
    """按终端显示宽度右侧补空格。"""
    padding = max(target_width - get_display_width(text), 0)
    return text + (" " * padding)


def print_info_table(rows: List[List[str]]) -> None:
    """用纯文本表格打印启动信息。"""
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
    """打印带留白和分隔线的终端信息块。"""
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


def print_soft_line(title: str, content: str, color: str = INFO_COLOR) -> None:
    """打印更简约的单行状态提示。"""
    print()
    print(color_text(title, color) + color_text(content, INFO_COLOR))


def build_default_export_path() -> Path:
    """生成默认的 Markdown 导出路径。"""
    filename = f"plan-context-{time.strftime('%Y%m%d-%H%M%S')}.md"
    return safe_resolve_path(filename)


DEFAULT_CONTEXT_WINDOW = 200000


MODEL_CONTEXT_WINDOWS = {
    "minimax-m2.5": 204800,
    "minimax-m2.5-highspeed": 204800,
}


@dataclass
class UsageSnapshot:
    """保存一次流式响应中最新的 usage 统计。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    updated_at: float = field(default_factory=time.time)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnMetrics:
    """保存一次 Agent 对话回合的性能统计。"""

    agent_name: str
    model: str
    started_at: float
    finished_at: float
    first_output_at: Optional[float] = None
    request_count: int = 0
    cumulative_prompt_tokens: int = 0
    cumulative_completion_tokens: int = 0
    cumulative_total_tokens: int = 0
    final_usage: Optional[UsageSnapshot] = None

    @property
    def elapsed_seconds(self) -> float:
        return max(self.finished_at - self.started_at, 0.0)

    @property
    def first_token_latency_seconds(self) -> Optional[float]:
        if self.first_output_at is None:
            return None
        return max(self.first_output_at - self.started_at, 0.0)

    @property
    def generation_seconds(self) -> Optional[float]:
        if self.first_output_at is None:
            return None
        return max(self.finished_at - self.first_output_at, 0.0)

    @property
    def output_tokens_per_second(self) -> Optional[float]:
        generation_seconds = self.generation_seconds
        if generation_seconds is None or generation_seconds <= 0:
            return None
        return self.cumulative_completion_tokens / generation_seconds


def get_optional_int_config(*keys: str) -> Optional[int]:
    """读取可选整数配置，优先配置文件，其次环境变量。"""
    for key in keys:
        config_value = RUNTIME_CONFIG.get(key)
        raw_value = config_value if config_value is not None else os.getenv(key)
        if raw_value in (None, ""):
            continue
        try:
            value = int(str(raw_value).strip())
        except (TypeError, ValueError):
            logger.warning("整数配置无效：%s=%r", key, raw_value)
            continue
        if value > 0:
            return value
    return None


def get_optional_bool_config(*keys: str) -> Optional[bool]:
    """读取可选布尔配置，优先配置文件，其次环境变量。"""
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
        logger.warning("布尔配置无效：%s=%r", key, raw_value)
    return None


def resolve_model_context_window(model_name: str) -> Optional[int]:
    """返回模型的上下文窗口大小，支持显式配置覆盖。"""
    configured = get_optional_int_config("OPENAI_CONTEXT_WINDOW", "MODEL_CONTEXT_WINDOW")
    if configured is not None:
        return configured
    return MODEL_CONTEXT_WINDOWS.get(model_name.strip().lower(), DEFAULT_CONTEXT_WINDOW)


OPENAI_ENABLE_THINKING = get_optional_bool_config("OPENAI_ENABLE_THINKING")
if OPENAI_ENABLE_THINKING is None:
    OPENAI_ENABLE_THINKING = True


def format_percent(numerator: int, denominator: Optional[int]) -> str:
    """格式化占比文本。"""
    if denominator is None or denominator <= 0:
        return "未知"
    percent = (numerator / denominator) * 100
    return f"{percent:.1f}%"


def build_progress_bar(numerator: int, denominator: Optional[int], width: int = 20) -> str:
    """构造终端展示用进度条。"""
    if denominator is None or denominator <= 0:
        return "[????????????????????]"
    ratio = max(0.0, min(numerator / denominator, 1.0))
    filled = int(round(ratio * width))
    return f"[{'#' * filled}{'-' * (width - filled)}]"


def get_system_name() -> str:
    """返回标准化后的当前操作系统名称。"""
    system = platform.system().lower()
    if system == "darwin":
        return "macOS"
    if system == "windows":
        return "Windows"
    if system == "linux":
        return "Linux"
    return platform.system() or "Unknown"


# ==== Prompt 模板 ====

# Plan / Execute 在 system prompt 里展示的可用工具分组（与 register_tool 保持一致，修改时只改此处）
PLAN_AGENT_AVAILABLE_TOOL_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "code_understanding",
        ("list_files", "search_code", "read_file_lines"),
    ),
    (
        "background_inspection",
        ("list_background_jobs", "read_background_job_log"),
    ),
    (
        "task_orchestration",
        ("task_plan", "execute_next_task"),
    ),
)

EXECUTE_AGENT_AVAILABLE_TOOL_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "code_understanding",
        ("list_files", "search_code", "read_file_lines"),
    ),
    (
        "code_editing",
        ("write_file", "replace_in_file", "edit_by_lines"),
    ),
    (
        "system_operations",
        (
            "run_command",
            "start_background_service",
            "sleep",
            "list_background_jobs",
            "read_background_job_log",
            "stop_background_job",
        ),
    ),
    (
        "task_status",
        ("read_tasks", "update_task"),
    ),
)


def build_available_tools_xml(
    groups: Sequence[Tuple[str, Sequence[str]]],
    *,
    base_indent: str = "  ",
) -> str:
    """根据分组构建带缩进的 <available_tools> XML 片段，供 Plan/Execute 系统提示复用。"""
    lines: List[str] = []
    lines.append(f"{base_indent}<available_tools>")
    for tag, tools in groups:
        lines.append(f"{base_indent}  <{tag}>")
        for name in tools:
            lines.append(f"{base_indent}    <tool>{name}</tool>")
        lines.append(f"{base_indent}  </{tag}>")
    lines.append(f"{base_indent}</available_tools>")
    return "\n".join(lines)


PLAN_AGENT_SYSTEM_PROMPT = f"""
<system>
  <role>
    你是任务规划 Agent（PlanAgent）。
    你负责理解用户需求、判断是否需要落地执行、拆分任务，并持续推动任务完成。
  </role>

  <identity_presentation>
    <rule>对用户统一自称“AI 编程助手”。</rule>
    <rule>除非用户明确询问系统内部架构或代理分工，否则不要主动提及 PlanAgent、ExecuteAgent 等内部角色名。</rule>
    <rule>当用户问“你是谁”时，回答你是 AI 编程助手，可帮助理解需求、修改代码、运行命令和推进任务完成。</rule>
  </identity_presentation>

  <primary_goal>
    在尽量少追问的前提下，生成清晰、可执行的任务列表，并把需要落地的请求推进到完成。
  </primary_goal>

  <hard_constraints>
    <rule>你只能直接调用自己已注册的工具；不要假装拥有不存在的直接执行能力。</rule>
    <rule>当你不能直接修改文件或运行命令时，不要说“做不到”就停下；如果可以委派给 ExecuteAgent，就应推动任务继续。</rule>
    <rule>不要重复创建已存在的任务，不要反复规划同一件事。</rule>
  </hard_constraints>

{build_available_tools_xml(PLAN_AGENT_AVAILABLE_TOOL_GROUPS)}

  <terminology>
    <rule>“任务(task)”指为完成用户目标而拆出的离散执行步骤。</rule>
    <rule>“后台作业(background_job)”指任务执行过程中启动的常驻进程、后台服务、watcher 或预览服务。</rule>
    <rule>后台作业不是任务，不参与 task 的 pending/running/done/failed 编排。</rule>
    <rule>当用户只说“任务”时，默认理解为 task；只有明确提到后台、服务、端口、日志、进程时，才理解为 background_job。</rule>
    <rule>若上下文仍不足以判断，先用一句话澄清：“你说的是执行任务，还是后台作业/后台服务？”</rule>
  </terminology>

  <decision_policy>
    <rule>如果用户只是寒暄、提问或闲聊，不要创建任务，直接回答即可。</rule>
    <rule>如果用户是在询问解释、方案、对比、建议或思路，且没有明确要求落地修改、运行命令或验证结果，优先直接回答，不要过早进入任务执行流。</rule>
    <rule>如果用户提出复杂需求，先使用工具查看项目结构、搜索相关代码、阅读必要文件，再决定如何拆分任务。</rule>
    <rule>如果请求需要真正落地，创建任务后应尽快调用 execute_next_task 开始执行，而不是停留在反复追问。</rule>
    <rule>如果用户明确表示“随便”“任意”“都行”“你决定”，说明用户已经授权你自行决定细节。对于低风险、低歧义、可安全落地的请求，应直接选择保守默认方案并执行。</rule>
  </decision_policy>

  <when_to_ask>
    <rule>只有在会覆盖已有重要文件、存在破坏性操作风险、或用户目标仍然无法安全执行时，才继续追问。</rule>
    <rule>如果低风险事项存在合理保守默认值，优先直接决策，而不是把选择权推回给用户。</rule>
  </when_to_ask>

  <tool_call_policy>
    <rule>先理解上下文，再规划任务；不要在没有任何检查的情况下直接规划复杂工作。</rule>
    <rule>如果用户是在查询当前后台作业状态、日志、端口或服务输出，优先直接使用只读查询工具回答；不要为纯查询请求额外创建执行任务。</rule>
    <rule>当你确认要拆分任务时，只调用一次 task_plan。</rule>
    <rule>如果当前会话里已经存在未完成的 request，不要再次调用 task_plan；应继续调用 execute_next_task 推进当前 request。</rule>
    <rule>调用 task_plan 时必须提供 request_summary，用一句简洁中文概括本轮用户真正想完成的目标。</rule>
    <rule>创建任务后，不要继续追加新的 task_plan；应转入执行和汇总，而不是重复规划。</rule>
  </tool_call_policy>

  <task_quality_rules>
    <rule>每个任务必须明确、可执行、粒度适中。</rule>
    <rule>任务描述应足够具体，让 ExecuteAgent 可以直接开始理解、修改、验证或运行命令。</rule>
    <rule>避免含糊任务，如“修复系统”“修改代码”。</rule>
  </task_quality_rules>

  <task_quality_examples>
    <good>查看 login.py 的实现</good>
    <good>搜索所有 login 相关代码</good>
    <good>修改 login.py 添加日志</good>
    <good>运行测试验证修改</good>
    <bad>修复系统</bad>
    <bad>修改代码</bad>
  </task_quality_examples>

  <execution_handoff>
    <rule>你可以在创建任务后调用 execute_next_task，把待办任务逐个交给 ExecuteAgent 执行。</rule>
    <rule>当 execute_next_task 返回还有待办任务时，继续调用 execute_next_task；当没有待办任务时，再向用户汇总最终结果。</rule>
    <rule>如果用户提到 replace_in_file、edit_by_lines、write_file、run_command 等执行类工具，或明确要求修改文件、运行命令、验证结果，不要仅因为你自己不能直接调用这些工具就说“没有这个工具”。应明确说明“我不能直接调用，但可以创建任务交给 ExecuteAgent 执行”，然后尽快使用 task_plan 和 execute_next_task 推动落地。</rule>
  </execution_handoff>

  <safe_defaults>
    <rule>即使工作区为空，也可以直接创建新文件；“工作区为空”不是拒绝执行的理由。</rule>
    <rule>创建简单示例文件时，默认放在工作区根目录。</rule>
    <rule>创建 Python 示例时，可默认命名为 example.py。</rule>
    <rule>文件内容应最小可用、可直接运行、便于用户理解。</rule>
  </safe_defaults>

  <output_contract>
    <rule>未开始规划时，不要假装已经执行过任务。</rule>
    <rule>任务仍在推进时，优先继续调用工具或执行下一步，而不是提前写大段总结。</rule>
    <rule>面向用户的默认输出应简洁直接；只有在存在风险、失败原因、关键假设或未验证事项时，才展开说明。</rule>
    <rule>只有当没有待办任务时，才向用户做最终汇总。</rule>
  </output_contract>
</system>
"""

EXECUTE_AGENT_SYSTEM_PROMPT = f"""
<system>
  <role>
    你是任务执行 Agent（ExecuteAgent）。
    你负责消费单个任务、实际执行操作，并反馈最终结果。
  </role>

  <identity_presentation>
    <rule>对用户统一自称“AI 编程助手”。</rule>
    <rule>除非用户明确询问系统内部架构或代理分工，否则不要主动提及 ExecuteAgent、PlanAgent 等内部角色名。</rule>
    <rule>如果结果会直接展示给用户，避免把内部角色名写进对外话术。</rule>
  </identity_presentation>

  <primary_goal>
    在不猜测、不偷懒、不虚构结果的前提下，尽最大可能把当前任务真实完成，并正确回写任务状态。
  </primary_goal>

  <continuity>
    <rule>同一轮用户请求中的多个任务属于同一个连续项目，你需要继承之前任务已经完成的工作，而不是把每个任务都当成全新项目。</rule>
  </continuity>

  <hard_constraints>
    <rule>不要假装读过未读文件、执行过未执行命令、验证过未验证结果。</rule>
    <rule>如果信息不足，先继续读取、搜索或检查，再执行修改。</rule>
    <rule>如果工具返回失败，不要假装成功；应根据现状重试、换策略，或如实失败。</rule>
  </hard_constraints>

  <task_input>
    <example>
任务：
修改 login.py 添加日志
    </example>
  </task_input>

{build_available_tools_xml(EXECUTE_AGENT_AVAILABLE_TOOL_GROUPS)}

  <terminology>
    <rule>“任务(task)”指当前需要完成的执行步骤。</rule>
    <rule>“后台作业(background_job)”指任务过程中启动的常驻进程、后台服务、watcher 或预览服务。</rule>
    <rule>后台作业不是任务；更新任务状态使用 update_task，查询后台作业使用 list_background_jobs / read_background_job_log / stop_background_job。</rule>
    <rule>当用户只说“任务”时，优先理解为 task；只有在明确提到后台、日志、端口、服务、进程时，才理解为 background_job。</rule>
  </terminology>

  <execution_process>
    <step>先理解任务。</step>
    <step>再查看相关代码或环境。</step>
    <step>然后进行修改或执行命令。</step>
    <step>最后验证结果。</step>
  </execution_process>

  <tool_call_policy>
    <rule>尽量使用工具，而不是猜测代码或假设文件内容。</rule>
    <rule>先收集完成任务所需的最小必要上下文，再做修改；不要盲改。</rule>
    <rule>能验证就验证；如果无法验证，要在结果中明确说明未验证的原因。</rule>
    <rule>如果仓库中已经存在明确的 lint、typecheck、test、build 或其他验证命令，在完成修改后优先运行与本次任务相关的最小必要验证。</rule>
    <rule>调用 run_command 时只运行非交互式命令；遇到脚手架、初始化器或包管理器命令，优先补上 --yes、-y、--no-interactive、--default 等参数，避免等待人工选择。</rule>
    <rule>如果需要确认当前任务队列、某个任务状态或历史结果，使用 read_tasks 工具。</rule>
    <rule>当任务是启动开发服务器、watcher、预览服务或其他常驻进程时，优先使用 start_background_service，而不是自己组合 run_command、sleep、read_background_job_log。</rule>
    <rule>调用 start_background_service 后，不要继续用 sleep 做无界轮询；应直接根据工具返回的 ready、timed_out、status 和 verification 判断下一步。</rule>
    <rule>如果需要等待后台服务启动、端口就绪或日志刷新，优先使用 sleep 工具；不要使用 timeout、ping、Start-Sleep 等命令充当等待手段。</rule>
    <rule>后台作业日志只能通过 read_background_job_log 查看。</rule>
  </tool_call_policy>

  <editing_strategy>
    <rule>修改代码前，先查看目标文件及其邻近实现，尽量沿用现有命名、结构、导入方式、错误处理和代码风格。</rule>
    <rule>不要假设新的第三方库、框架能力或工程约定已经存在；如果需要使用它们，先通过代码或配置确认仓库里确实已有相关依赖或模式。</rule>
    <rule>如果需要修改代码，优先使用 replace_in_file 做唯一文本块替换；只有在你已经明确知道并核对过“要被替换的完整连续行区间”时，才使用 edit_by_lines；仅在需要新建文件或整体重写时使用 write_file。</rule>
    <rule>调用 replace_in_file 时，old_string 应包含足够的上下文，且必须保证在文件中唯一匹配；如果不唯一，应先继续读取更多上下文，再重试。</rule>
    <rule>调用 edit_by_lines 前，应先用 read_file_lines 读取并确认目标行范围和当前内容，避免基于猜测修改。</rule>
    <rule>调用 edit_by_lines 时，必须把刚读到的精确旧内容通过 old_text 一并传入；如果当前文件内容与 old_text 不一致，应停止写入并重新读取。</rule>
    <rule>如果你修改的是 Vue/HTML/JSX/模板等嵌套结构，且需要连同上下文容器一起调整，优先使用 replace_in_file；不要只替换一两行，却把未落在行区间内的外围标签再次写进 new_text。</rule>
    <rule>如果一种编辑策略失败，先分析失败原因，再选择更合适的下一种工具，而不是盲目重复同一步。</rule>
  </editing_strategy>

  <failure_handling>
    <rule>如果遇到缺少文件、命令失败、内容不匹配、权限受限等情况，应基于当前证据判断是否可继续推进。</rule>
    <rule>如果问题可通过补充读取、缩小范围、调整命令或更换编辑方式解决，应先继续尝试。</rule>
    <rule>只有在任务确实无法安全完成时，才将任务标记为 failed。</rule>
  </failure_handling>

  <completion_rules>
    <rule>当任务完成时，必须调用 update_task，并将 status 设为 "done"。</rule>
    <rule>如果任务失败，必须调用 update_task，并将 status 设为 "failed"。</rule>
    <rule>不要在未调用 update_task 的情况下就认为任务已经结束。</rule>
  </completion_rules>

  <output_contract>
    <rule>调用 update_task 后，提供简短清晰的执行结果，不要继续长篇发挥。</rule>
    <rule>结果应优先说明做了什么、是否验证、最终状态是什么。</rule>
    <rule>如果修改内容较简单且验证正常，默认用最短可理解结果回复，不额外展开实现细节。</rule>
    <rule>如果未验证成功，要明确说明未验证原因，而不是给出模糊总结。</rule>
  </output_contract>
</system>
"""


def get_now_time_text() -> str:
    """返回当前本地时间文本，用于注入运行时上下文。"""
    return time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime())


def format_timestamp(timestamp: Any) -> str:
    """将时间戳格式化为本地时间文本。"""
    if not isinstance(timestamp, (int, float)):
        return "未知"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def format_duration(seconds: Optional[float]) -> str:
    """把秒数格式化为适合终端阅读的文本。"""
    if seconds is None:
        return "未知"
    return f"{max(seconds, 0.0):.2f}s"


def format_token_speed(tokens: int, seconds: Optional[float]) -> str:
    """格式化平均 token 输出速度。"""
    if seconds is None or seconds <= 0 or tokens <= 0:
        return "未知"
    return f"{tokens / seconds:.1f} tokens/s"


def format_token_count(value: int) -> str:
    """格式化 token 数量，增强可读性。"""
    return f"{max(int(value), 0):,}"


def format_history_message_content(content: Any) -> str:
    """把消息内容格式化为适合导出的文本。"""
    if content is None or content == "":
        return "<empty>"
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, indent=2)


def build_runtime_context_xml(
    agent_name: str,
    model_name: str,
    execution_mode: str,
) -> str:
    """构造注入到 system prompt 中的运行时环境信息。"""
    return "\n".join(
        [
            "  <runtime_context>",
            f"    <agent_name>{escape(agent_name)}</agent_name>",
            f"    <model>{escape(model_name)}</model>",
            f"    <execution_mode>{escape(execution_mode)}</execution_mode>",
            f"    <now_time>{escape(get_now_time_text())}</now_time>",
            f"    <shell>{escape(SHELL_NAME)}</shell>",
            f"    <workspace_dir>{escape(str(WORKSPACE_DIR))}</workspace_dir>",
            f"    <is_git_repo>{str(IS_GIT_REPO).lower()}</is_git_repo>",
            "  </runtime_context>",
        ]
    )


def build_project_instructions_xml() -> str:
    """构造项目级指令，明确其来自用户对项目的要求。"""
    if not PROJECT_INSTRUCTIONS_TEXT:
        return ""
    return "\n".join(
        [
            "  <project_instructions>",
            "    <source>AGENTS.md</source>",
            "    <owner>user</owner>",
            "    <meaning>这是用户对当前项目的要求与约定，不是普通参考信息。</meaning>",
            (
                "    <priority>"
                "若与本轮用户明确指令冲突，以本轮用户指令为准；否则应优先遵守这些项目要求。"
                "</priority>"
            ),
            "    <content>",
            escape(PROJECT_INSTRUCTIONS_TEXT),
            "    </content>",
            "  </project_instructions>",
        ]
    )


def build_execute_task_prompt_xml(
    *,
    task_id: str,
    request_id: str,
    request_summary: str,
    request_user_input: str,
    task_description: str,
    previous_task_summary: str,
) -> str:
    """构造发给 ExecuteAgent 的结构化任务输入。"""
    lines = [
        "<task_execution_input>",
        "  <task_context>",
        f"    <task_id>{escape(task_id)}</task_id>",
        f"    <request_id>{escape(request_id)}</request_id>",
        f"    <request_summary>{escape(request_summary)}</request_summary>",
    ]
    if request_user_input:
        lines.append(
            f"    <request_user_input>{escape(request_user_input)}</request_user_input>"
        )
    lines.extend(
        [
            f"    <task_description>{escape(task_description)}</task_description>",
            "  </task_context>",
            "  <completed_task_summary>",
            escape(previous_task_summary),
            "  </completed_task_summary>",
            "  <execution_rules>",
            "    <rule>你正在延续同一个项目，请基于当前工作区现状和上述已完成任务继续执行，不要从零假设整个项目。</rule>",
            "    <rule>任务状态只以本任务输入和 update_task 工具为准。</rule>",
            "    <rule>如果本任务需要启动开发服务器、预览服务、watcher 或其他常驻进程，优先使用 start_background_service，不要自己拼 run_command + sleep + read_background_job_log 的轮询。</rule>",
            "    <rule>如果需要查看后台作业日志，只能使用 read_background_job_log。</rule>",
            "    <rule>如果需要等待服务启动、日志刷新或端口就绪，使用 sleep 工具，不要运行 timeout、ping、sleep、Start-Sleep 等等待命令。</rule>",
            "    <rule>执行完成后请调用 update_task 更新最终状态。调用后不要继续长篇总结。</rule>",
            "  </execution_rules>",
            "</task_execution_input>",
        ]
    )
    return "\n".join(lines)


def with_runtime_context(
    base_prompt: str,
    *,
    agent_name: str,
    model_name: str,
    execution_mode: str,
) -> str:
    """把运行时上下文前置插入，并补充项目级要求。"""
    project_instructions_xml = build_project_instructions_xml()
    runtime_context_xml = build_runtime_context_xml(
        agent_name,
        model_name,
        execution_mode,
    )
    prompt_with_runtime = base_prompt
    if "</primary_goal>" in prompt_with_runtime:
        prompt_with_runtime = prompt_with_runtime.replace(
            "</primary_goal>",
            f"</primary_goal>\n\n{runtime_context_xml}",
            1,
        )
    else:
        prompt_with_runtime = prompt_with_runtime.replace(
            "</system>",
            f"{runtime_context_xml}\n</system>",
            1,
        )

    if not project_instructions_xml:
        return prompt_with_runtime

    return prompt_with_runtime.replace(
        "</system>",
        f"\n{project_instructions_xml}\n</system>",
        1,
    )


# ==== 路径安全 ====


def safe_resolve_path(user_path: str) -> Path:
    """将用户输入路径解析到工作区内，越界时直接拒绝。"""

    abs_path = (WORKSPACE_DIR / user_path).resolve()

    try:
        abs_path.relative_to(WORKSPACE_DIR)
    except ValueError:
        raise PermissionError("Path outside workspace")

    for reserved_dir in (_AGENT_DIR.resolve(), _LOG_ROOT_DIR.resolve()):
        try:
            abs_path.relative_to(reserved_dir)
        except ValueError:
            continue
        raise PermissionError("Path inside runtime directory is reserved")

    return abs_path


def to_workspace_relative(path: Path) -> str:
    """把绝对路径转换成相对工作区的显示路径。"""
    return str(path.resolve().relative_to(WORKSPACE_DIR))


IGNORED_PATH_PARTS = {
    ".agent",
    ".logs",
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
}


def should_ignore_path(path: Path, root: Path) -> bool:
    """判断路径是否应被扫描流程忽略。"""
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = path.resolve().relative_to(WORKSPACE_DIR)
    return any(part in IGNORED_PATH_PARTS for part in rel.parts)


def build_workspace_ignore_spec() -> Any:
    """基于 .gitignore 和内置规则构造 pathspec 匹配器。"""
    if pathspec is None:
        return None

    patterns: List[str] = []

    def append_pattern_variants(raw_pattern: str) -> None:
        pattern = raw_pattern.strip()
        if not pattern or pattern.startswith("#"):
            return
        patterns.append(pattern)
        if (
            not pattern.startswith("!")
            and not pattern.endswith("/")
            and all(ch not in pattern for ch in "*?[]")
        ):
            patterns.append(f"{pattern}/")

    gitignore_path = WORKSPACE_DIR / ".gitignore"
    if gitignore_path.exists():
        try:
            for line in gitignore_path.read_text(encoding="utf-8").splitlines():
                append_pattern_variants(line)
        except OSError:
            pass

    for part in sorted(IGNORED_PATH_PARTS):
        append_pattern_variants(part)

    return pathspec.GitIgnoreSpec.from_lines(patterns)


# ==== 工具基类 ====


class BaseTool:
    """所有工具的统一抽象基类。"""

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}

    def run(self, parameters: Dict[str, Any]) -> str:
        raise NotImplementedError

    def success(self, data: Any) -> str:
        return json.dumps(
            {"success": True, "data": data, "error": None},
            ensure_ascii=False,
        )

    def fail(self, msg: str) -> str:
        return json.dumps(
            {"success": False, "data": None, "error": msg},
            ensure_ascii=False,
        )

    def to_dict(self) -> Dict[str, Any]:

        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class TaskRecord:
    """单个任务的持久化记录。"""
    id: str
    description: str
    request_id: Optional[str] = None
    session_id: Optional[str] = None
    status: str = "pending"
    result: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_storage_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        *,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> "TaskRecord":
        return cls(
            id=data["id"],
            description=data["description"],
            request_id=data.get("request_id", request_id),
            session_id=data.get("session_id", session_id),
            status=data.get("status", "pending"),
            result=data.get("result"),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
        )


@dataclass
class RequestRecord:
    """单次用户请求的持久化记录。"""

    id: str
    session_id: Optional[str] = None
    summary: str = ""
    user_input: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tasks: List[TaskRecord] = field(default_factory=list)

    def compute_status(self) -> str:
        if not self.tasks:
            return "pending"
        if any(task.status == "running" for task in self.tasks):
            return "running"
        if any(task.status == "pending" for task in self.tasks):
            return "pending"
        if any(task.status == "failed" for task in self.tasks):
            return "failed"
        return "done"

    def has_active_tasks(self) -> bool:
        return any(task.status in {"pending", "running"} for task in self.tasks)

    def to_storage_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "summary": self.summary,
            "user_input": self.user_input,
            "status": self.compute_status(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tasks": [
                task.to_storage_dict()
                for task in sorted(self.tasks, key=lambda item: item.created_at)
            ],
        }


class TaskStore:
    """负责加载、保存和管理请求与任务状态。"""

    def __init__(self, storage_path: Path = _TASK_FILE) -> None:
        self.storage_path = storage_path
        self._requests: Dict[str, RequestRecord] = {}
        self._tasks: Dict[str, TaskRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.storage_path.exists():
            return

        try:
            content = self.storage_path.read_text(encoding="utf-8").strip()
            raw = {"requests": []} if not content else json.loads(content)
        except Exception:
            logger.exception("加载 task.json 失败")
            return

        self._requests.clear()
        self._tasks.clear()

        if isinstance(raw, list):
            self._load_legacy_tasks(raw)
            return

        requests = raw.get("requests") if isinstance(raw, dict) else None
        if not isinstance(requests, list):
            logger.warning("task.json 格式无效，已忽略")
            return

        for item in requests:
            if not isinstance(item, dict):
                continue
            request_id = str(item.get("id", "")).strip() or str(uuid.uuid4())[:8]
            request = RequestRecord(
                id=request_id,
                session_id=item.get("session_id"),
                summary=str(item.get("summary", "")).strip(),
                user_input=item.get("user_input"),
                created_at=item.get("created_at", time.time()),
                updated_at=item.get("updated_at", time.time()),
            )
            raw_tasks = item.get("tasks")
            if not isinstance(raw_tasks, list):
                raw_tasks = []
            for raw_task in raw_tasks:
                if not isinstance(raw_task, dict):
                    continue
                try:
                    task = TaskRecord.from_dict(
                        raw_task,
                        request_id=request.id,
                        session_id=request.session_id,
                    )
                except KeyError:
                    continue
                request.tasks.append(task)
                self._tasks[task.id] = task
            self._requests[request.id] = request

    def _load_legacy_tasks(self, raw_tasks: List[Any]) -> None:
        legacy_groups: Dict[Optional[str], List[TaskRecord]] = {}
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            try:
                task = TaskRecord.from_dict(item)
            except KeyError:
                continue
            legacy_groups.setdefault(task.session_id, []).append(task)

        for session_id, tasks in legacy_groups.items():
            if not tasks:
                continue
            tasks.sort(key=lambda item: item.created_at)
            now = time.time()
            request = RequestRecord(
                id=f"legacy-{tasks[0].id}",
                session_id=session_id,
                summary="历史任务迁移（缺少原始请求摘要）",
                created_at=min((task.created_at for task in tasks), default=now),
                updated_at=max((task.updated_at for task in tasks), default=now),
            )
            for task in tasks:
                task.request_id = request.id
                request.tasks.append(task)
                self._tasks[task.id] = task
            self._requests[request.id] = request

    def _save(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "requests": [
                request.to_storage_dict()
                for request in sorted(
                    self._requests.values(), key=lambda item: item.created_at
                )
            ]
        }
        temp_path = self.storage_path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.storage_path)

    def reset(self) -> None:
        self._requests.clear()
        self._tasks.clear()
        self._save()

    def _iter_tasks(
        self,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> List[TaskRecord]:
        tasks = sorted(self._tasks.values(), key=lambda task: task.created_at)
        if request_id is not None:
            tasks = [task for task in tasks if task.request_id == request_id]
        if session_id is None:
            return tasks
        return [task for task in tasks if task.session_id == session_id]

    def _iter_requests(self, session_id: Optional[str] = None) -> List[RequestRecord]:
        requests = sorted(self._requests.values(), key=lambda item: item.created_at)
        if session_id is None:
            return requests
        return [request for request in requests if request.session_id == session_id]

    def _find_reusable_request(
        self,
        session_id: Optional[str],
        request_summary: str,
        user_input: Optional[str],
    ) -> Optional[RequestRecord]:
        for request in reversed(self._iter_requests(session_id)):
            if request.summary != request_summary:
                continue
            if (request.user_input or None) != (user_input or None):
                continue
            if request.has_active_tasks():
                return request
        return None

    def _build_task_dict(self, task: TaskRecord) -> Dict[str, Any]:
        data = task.to_dict()
        request = self._requests.get(task.request_id or "")
        data["request_summary"] = request.summary if request else ""
        data["user_input"] = request.user_input if request else None
        return data

    def get_task_dict(self, task_id: str) -> Optional[Dict[str, Any]]:
        task = self.get(task_id)
        if task is None:
            return None
        return self._build_task_dict(task)

    def _build_request_dict(self, request: RequestRecord) -> Dict[str, Any]:
        return {
            "id": request.id,
            "session_id": request.session_id,
            "summary": request.summary,
            "user_input": request.user_input,
            "status": request.compute_status(),
            "created_at": request.created_at,
            "updated_at": request.updated_at,
            "tasks": [self._build_task_dict(task) for task in self._iter_tasks(request_id=request.id)],
        }

    def get_active_request(self, session_id: Optional[str] = None) -> Optional[RequestRecord]:
        for request in self._iter_requests(session_id):
            if request.has_active_tasks():
                return request
        return None

    def has_active_request(self, session_id: Optional[str] = None) -> bool:
        return self.get_active_request(session_id) is not None

    def create_tasks(
        self,
        raw_tasks: List[Any],
        session_id: Optional[str] = None,
        request_summary: str = "",
        user_input: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        created: List[Dict[str, Any]] = []
        normalized_summary = str(request_summary).strip()
        normalized_user_input = str(user_input).strip() if user_input is not None else None
        request = self._find_reusable_request(
            session_id,
            normalized_summary,
            normalized_user_input,
        )
        created_request = False
        if request is None:
            now = time.time()
            request = RequestRecord(
                id=str(uuid.uuid4())[:8],
                session_id=session_id,
                summary=normalized_summary,
                user_input=normalized_user_input,
                created_at=now,
                updated_at=now,
            )
            self._requests[request.id] = request
            created_request = True

        for raw_task in raw_tasks:
            if isinstance(raw_task, dict):
                description = str(raw_task.get("description", "")).strip()
            else:
                description = str(raw_task).strip()

            if not description:
                continue

            if any(
                task.description == description for task in request.tasks
            ):
                continue

            task = TaskRecord(
                id=str(uuid.uuid4())[:8],
                description=description,
                request_id=request.id,
                session_id=session_id,
            )
            self._tasks[task.id] = task
            request.tasks.append(task)
            request.updated_at = time.time()
            created.append(self._build_task_dict(task))

        if created_request and not request.tasks:
            self._requests.pop(request.id, None)
        self._save()
        return created

    def list_tasks(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return [self._build_task_dict(task) for task in self._iter_tasks(session_id)]

    def list_requests(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return [self._build_request_dict(request) for request in self._iter_requests(session_id)]

    def get(self, task_id: str) -> Optional[TaskRecord]:
        return self._tasks.get(task_id)

    def get_request(self, request_id: str) -> Optional[RequestRecord]:
        return self._requests.get(request_id)

    def get_next_pending(self, session_id: Optional[str] = None) -> Optional[TaskRecord]:
        active_request = self.get_active_request(session_id)
        if active_request is None:
            return None
        for task in self._iter_tasks(session_id, request_id=active_request.id):
            if task.status == "pending":
                return task
        return None

    def pending_tasks(
        self,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return [
            self._build_task_dict(task)
            for task in self._iter_tasks(session_id, request_id=request_id)
            if task.status == "pending"
        ]

    def completed_tasks(
        self,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return [
            self._build_task_dict(task)
            for task in self._iter_tasks(session_id, request_id=request_id)
            if task.status in {"done", "failed"}
        ]

    def has_active_tasks(self, session_id: Optional[str] = None) -> bool:
        return any(
            task.status in {"pending", "running"}
            for task in self._iter_tasks(session_id)
        )

    def update_task(
        self, task_id: str, status: str, result: Optional[str] = None
    ) -> Dict[str, Any]:
        if status not in TASK_STATUS:
            raise ValueError("invalid status")

        task = self.get(task_id)
        if not task:
            raise KeyError("task not found")

        task.status = status
        if result is not None:
            task.result = result
        task.updated_at = time.time()
        request = self.get_request(task.request_id or "")
        if request is not None:
            request.updated_at = task.updated_at
        self._save()
        return self._build_task_dict(task)


@dataclass
class BackgroundJobRecord:
    """后台作业的持久化记录。"""

    id: str
    command: str
    pid: int
    pid_role: str = "launcher"
    cwd: str = ""
    status: str = "running"
    stdout_log: str = ""
    stderr_log: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    stopped_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BackgroundJobRecord":
        return cls(
            id=data["id"],
            command=data["command"],
            pid=int(data["pid"]),
            pid_role=data.get("pid_role", "launcher"),
            cwd=data.get("cwd", ""),
            status=data.get("status", "running"),
            stdout_log=data.get("stdout_log", ""),
            stderr_log=data.get("stderr_log", ""),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            stopped_at=data.get("stopped_at"),
        )


class BackgroundJobStore:
    """负责持久化后台作业注册表。"""

    def __init__(
        self,
        storage_path: Path = _BACKGROUND_JOBS_FILE,
        log_dir: Path = _BACKGROUND_LOG_DIR,
    ) -> None:
        self.storage_path = storage_path
        self.log_dir = log_dir
        self._jobs: Dict[str, BackgroundJobRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.storage_path.exists():
            return

        try:
            content = self.storage_path.read_text(encoding="utf-8").strip()
            raw = [] if not content else json.loads(content)
        except Exception:
            logger.exception("加载 background_jobs.json 失败")
            return

        if not isinstance(raw, list):
            logger.warning("background_jobs.json 格式无效，已忽略")
            return

        self._jobs.clear()
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                job = BackgroundJobRecord.from_dict(item)
            except (KeyError, TypeError, ValueError):
                continue
            self._jobs[job.id] = job

    def _save(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        payload = [
            job.to_dict()
            for job in sorted(self._jobs.values(), key=lambda item: item.created_at)
        ]
        temp_path = self.storage_path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.storage_path)

    def clear_all(self) -> int:
        """清空后台作业注册表并删除所有后台日志文件，返回删除的日志文件数。"""
        count = 0
        if self.log_dir.exists():
            for p in self.log_dir.iterdir():
                if p.is_file():
                    try:
                        p.unlink()
                        count += 1
                    except OSError:
                        logger.warning("删除日志文件失败：%s", p)
        self._jobs.clear()
        self._save()
        return count

    def create_job(
        self,
        *,
        job_id: Optional[str] = None,
        command: str,
        pid: int,
        pid_role: str,
        cwd: Path,
        stdout_log: Path,
        stderr_log: Path,
    ) -> Dict[str, Any]:
        now = time.time()
        job = BackgroundJobRecord(
            id=job_id or str(uuid.uuid4())[:8],
            command=command,
            pid=pid,
            pid_role=pid_role,
            cwd=str(cwd),
            stdout_log=str(stdout_log),
            stderr_log=str(stderr_log),
            created_at=now,
            updated_at=now,
        )
        self._jobs[job.id] = job
        self._save()
        return job.to_dict()

    def get(self, job_id: str) -> Optional[BackgroundJobRecord]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> List[BackgroundJobRecord]:
        return sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)

    def update_status(
        self,
        job_id: str,
        status: str,
        *,
        stopped_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        job = self.get(job_id)
        if not job:
            raise KeyError("background job not found")

        job.status = status
        job.updated_at = time.time()
        if stopped_at is not None:
            job.stopped_at = stopped_at
        self._save()
        return job.to_dict()

    def refresh_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self.get(job_id)
        if not job:
            return None
        if job.status == "running" and not is_process_running(job.pid):
            self.update_status(job.id, "exited", stopped_at=time.time())
            job = self.get(job_id)
        return job.to_dict() if job else None

    def refresh_jobs(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        jobs = self.list_jobs()
        if limit is not None:
            jobs = jobs[:limit]
        refreshed: List[Dict[str, Any]] = []
        changed = False
        for job in jobs:
            if job.status == "running" and not is_process_running(job.pid):
                job.status = "exited"
                job.stopped_at = time.time()
                job.updated_at = time.time()
                changed = True
            refreshed.append(job.to_dict())
        if changed:
            self._save()
        return refreshed


class PlanHistoryStore:
    """负责持久化 PlanAgent 的上下文历史。"""

    def __init__(self, storage_path: Path = _HISTORY_FILE) -> None:
        self.storage_path = storage_path
        self._sessions: List[Dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.storage_path.exists():
            return

        try:
            content = self.storage_path.read_text(encoding="utf-8").strip()
            raw = {"sessions": []} if not content else json.loads(content)
        except Exception:
            logger.exception("加载 history.json 失败")
            return

        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        if not isinstance(sessions, list):
            logger.warning("history.json 格式无效，已忽略")
            return

        normalized_sessions: List[Dict[str, Any]] = []
        changed = False
        for item in sessions:
            if not isinstance(item, dict):
                continue
            session = dict(item)
            if "status" in session:
                session.pop("status", None)
                changed = True
            normalized_sessions.append(session)

        self._sessions = normalized_sessions
        if changed:
            self._save()

    def clear_all(self) -> None:
        """清空所有历史会话。"""
        self._sessions.clear()
        self._save()

    def _save(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"sessions": self._sessions}
        temp_path = self.storage_path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.storage_path)

    def _copy_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return json.loads(json.dumps(messages, ensure_ascii=False))

    def _find_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        for session in self._sessions:
            if session.get("id") == session_id:
                return session
        return None

    def start_session(self, agent_name: str, messages: List[Dict[str, Any]]) -> str:
        now = time.time()
        session_id = str(uuid.uuid4())
        self._sessions.append(
            {
                "id": session_id,
                "agent_name": agent_name,
                "created_at": now,
                "updated_at": now,
                "message_count": len(messages),
                "messages": self._copy_messages(messages),
            }
        )
        self._save()
        return session_id

    def sync_session(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
    ) -> None:
        session = self._find_session(session_id)
        if session is None:
            raise KeyError(f"history session not found: {session_id}")

        session["messages"] = self._copy_messages(messages)
        session["message_count"] = len(messages)
        session["updated_at"] = time.time()
        session.pop("status", None)
        self._save()

    def list_sessions(self) -> List[Dict[str, Any]]:
        return json.loads(json.dumps(self._sessions, ensure_ascii=False))

    def export_markdown(
        self,
        output_path: Path,
        current_session_id: Optional[str] = None,
        only_session_id: Optional[str] = None,
    ) -> None:
        sessions = self.list_sessions()
        export_scope = "所有对话"
        if only_session_id is not None:
            sessions = [
                session for session in sessions if session.get("id") == only_session_id
            ]
            export_scope = "当前对话"
        lines = [
            "# PlanAgent 上下文导出",
            "",
            f"- 导出时间：{get_now_time_text()}",
            f"- 导出范围：{export_scope}",
            f"- 历史文件：`{_HISTORY_FILE}`",
            f"- 会话数量：{len(sessions)}",
            "",
        ]

        if not sessions:
            lines.append("_当前没有可导出的 PlanAgent 上下文。_")
        else:
            for index, session in enumerate(sessions, start=1):
                lines.extend(
                    [
                        f"## 会话 {index}",
                        "",
                        f"- 会话 ID：`{session.get('id', '')}`",
                        f"- Agent：`{session.get('agent_name', 'PlanAgent')}`",
                        f"- 创建时间：{format_timestamp(session.get('created_at'))}",
                        f"- 更新时间：{format_timestamp(session.get('updated_at'))}",
                        f"- 消息数量：{session.get('message_count', 0)}",
                        "",
                    ]
                )

                messages = session.get("messages") or []
                if not messages:
                    lines.extend(["_该会话暂无消息。_", ""])
                    continue

                for message_index, message in enumerate(messages, start=1):
                    role = str(message.get("role", "unknown"))
                    lines.append(f"### {message_index}. `{role}`")
                    tool_call_id = message.get("tool_call_id")
                    if tool_call_id:
                        lines.append(f"- tool_call_id: `{tool_call_id}`")

                    tool_calls = message.get("tool_calls")
                    if tool_calls:
                        lines.extend(
                            [
                                "",
                                "```json",
                                json.dumps(tool_calls, ensure_ascii=False, indent=2),
                                "```",
                            ]
                        )

                    reasoning_content = message.get("reasoning_content")
                    if reasoning_content not in (None, ""):
                        lines.extend(
                            [
                                "",
                                "#### reasoning_content",
                                "",
                                "```text",
                                format_history_message_content(reasoning_content),
                                "```",
                            ]
                        )

                    lines.extend(
                        [
                            "",
                            "```text",
                            format_history_message_content(message.get("content")),
                            "```",
                            "",
                        ]
                    )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# ==== 文件导航工具 ====


class ListFilesTool(BaseTool):
    """列出工作区内目录或文件。"""

    name = "list_files"
    description = "List files in directory"

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path, or absolute if it resolves under the workspace; outside rejected. Default \".\".",
            },
            "depth": {
                "type": "integer",
                "description": "Max depth below path (default 3).",
            },
            "type": {
                "type": "string",
                "description": "Entry filter: all | file | directory.",
            },
            "glob": {
                "type": "string",
                "description": "Optional fnmatch pattern on paths relative to path.",
            },
            "limit": {
                "type": "integer",
                "description": "Max entries returned (default 200).",
            },
        },
    }

    def run(self, parameters: Dict[str, Any]) -> str:

        path = parameters.get("path", ".")
        depth = parameters.get("depth", 3)
        entry_type = parameters.get("type", "all")
        glob_pattern = parameters.get("glob")
        limit = parameters.get("limit", 200)

        try:

            root = safe_resolve_path(path)
            if not root.exists():
                return self.fail("path not found")

            if entry_type not in {"all", "file", "directory"}:
                return self.fail("type must be one of: all, file, directory")

            if limit < 1:
                return self.fail("limit must be >= 1")

            if root.is_file():
                item_type = "file"
                rel = root.relative_to(WORKSPACE_DIR)
                if entry_type not in {"all", "file"}:
                    return self.success([])
                if glob_pattern and not fnmatch.fnmatch(root.name, glob_pattern):
                    return self.success([])
                return self.success([{"path": str(rel), "type": item_type}])

            results = []

            for p in root.rglob("*"):
                if should_ignore_path(p, root):
                    continue

                rel = p.relative_to(WORKSPACE_DIR)
                rel_to_root = p.relative_to(root)

                if len(rel_to_root.parts) > depth:
                    continue

                item_type = "directory" if p.is_dir() else "file"

                if entry_type != "all" and item_type != entry_type:
                    continue

                if glob_pattern and not fnmatch.fnmatch(str(rel_to_root), glob_pattern):
                    continue

                results.append(
                    {
                        "path": str(rel),
                        "type": item_type,
                    }
                )

            results.sort(key=lambda item: (item["type"] != "directory", item["path"]))

            return self.success(results[:limit])

        except Exception as e:
            return self.fail(str(e))


# ==== 搜索工具 ====


class SearchCodeTool(BaseTool):
    """基于纯 Python 遍历在代码中查找关键字。"""

    name = "search_code"
    description = "Search keyword in codebase"

    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Literal substring or regex pattern (see regex).",
            },
            "max_results": {
                "type": "integer",
                "description": "Max matches to return (default 20).",
            },
            "path": {
                "type": "string",
                "description": "Workspace-relative path, or absolute if it resolves under the workspace; outside rejected. Default \".\".",
            },
            "glob": {
                "type": "string",
                "description": "Optional fnmatch filter on file paths under target.",
            },
            "regex": {
                "type": "boolean",
                "description": "If true, query is regex; else substring.",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Substring match only; ignored when regex (default true).",
            },
        },
        "required": ["query"],
    }

    def _should_ignore_search_path(self, path: Path, ignore_spec: Any) -> bool:
        """判断搜索时是否应忽略该路径。"""
        if ignore_spec is not None:
            try:
                relative = path.resolve().relative_to(WORKSPACE_DIR).as_posix()
            except ValueError:
                return True
            candidates = [relative]
            if path.is_dir():
                candidates.append(f"{relative}/")
            return any(ignore_spec.match_file(candidate) for candidate in candidates)
        return should_ignore_path(path, WORKSPACE_DIR)

    def _iter_search_files(
        self,
        target: Path,
        *,
        glob_pattern: Optional[str],
        ignore_spec: Any,
    ):
        """按稳定顺序遍历待搜索文件。"""
        if target.is_file():
            if self._should_ignore_search_path(target, ignore_spec):
                return
            if glob_pattern and not fnmatch.fnmatch(target.name, glob_pattern):
                return
            yield target
            return

        stack: List[Path] = [target]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as scanner:
                    entries = sorted(
                        scanner,
                        key=lambda entry: (
                            not entry.is_dir(follow_symlinks=False),
                            entry.name,
                        ),
                    )
            except OSError:
                continue

            child_dirs: List[Path] = []
            for entry in entries:
                entry_path = Path(entry.path)
                if self._should_ignore_search_path(entry_path, ignore_spec):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    child_dirs.append(entry_path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                rel_to_target = entry_path.relative_to(target).as_posix()
                if glob_pattern and not fnmatch.fnmatch(rel_to_target, glob_pattern):
                    continue
                yield entry_path

            for child_dir in reversed(child_dirs):
                stack.append(child_dir)

    def _match_line(
        self,
        line: str,
        query: str,
        *,
        regex: bool,
        case_sensitive: bool,
        compiled_pattern: Optional[re.Pattern[str]] = None,
    ) -> bool:
        """判断单行文本是否命中查询。"""
        if regex:
            if compiled_pattern is None:
                return False
            return compiled_pattern.search(line) is not None
        if case_sensitive:
            return query in line
        return query.lower() in line.lower()

    def _search_with_python(
        self,
        target: Path,
        *,
        query: str,
        max_results: int,
        glob_pattern: Optional[str],
        regex: bool,
        case_sensitive: bool,
    ) -> List[Dict[str, Any]]:
        """使用纯 Python 流式搜索文件内容。"""
        results: List[Dict[str, Any]] = []
        regex_flags = 0 if case_sensitive else re.IGNORECASE
        compiled_pattern = re.compile(query, regex_flags) if regex else None
        ignore_spec = build_workspace_ignore_spec()

        for file_path in self._iter_search_files(
            target,
            glob_pattern=glob_pattern,
            ignore_spec=ignore_spec,
        ):
            try:
                handle = file_path.open("r", encoding="utf-8", errors="ignore")
            except Exception:
                continue

            workspace_relative = str(file_path.resolve().relative_to(WORKSPACE_DIR))
            with handle:
                for line_number, line in enumerate(handle, start=1):
                    snippet = line.rstrip("\r\n")
                    if not self._match_line(
                        snippet,
                        query,
                        regex=regex,
                        case_sensitive=case_sensitive,
                        compiled_pattern=compiled_pattern,
                    ):
                        continue

                    results.append(
                        {
                            "file": workspace_relative,
                            "line": line_number,
                            "snippet": snippet,
                        }
                    )
                    if len(results) >= max_results:
                        return results

        return results

    def run(self, parameters: Dict[str, Any]) -> str:

        query = parameters["query"]
        max_results = parameters.get("max_results", 20)
        path = parameters.get("path", ".")
        glob_pattern = parameters.get("glob")
        regex = parameters.get("regex", False)
        case_sensitive = parameters.get("case_sensitive", True)

        try:
            if max_results < 1:
                return self.fail("max_results must be >= 1")

            target = safe_resolve_path(path)
            if not target.exists():
                return self.fail("path not found")

            if regex:
                try:
                    re.compile(query, 0 if case_sensitive else re.IGNORECASE)
                except re.error as exc:
                    return self.fail(f"invalid regex: {exc}")

            results = self._search_with_python(
                target,
                query=query,
                max_results=max_results,
                glob_pattern=glob_pattern,
                regex=regex,
                case_sensitive=case_sensitive,
            )

            return self.success(results)

        except Exception as e:
            return self.fail(str(e))


# ==== 文件读取工具 ====


class ReadFileLinesTool(BaseTool):
    """按行读取文件内容。"""

    name = "read_file_lines"
    description = "Read lines from file"

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path, or absolute if it resolves under the workspace; outside rejected.",
            },
            "start_line": {
                "type": "integer",
                "description": "1-based start line (default 1).",
            },
            "end_line": {
                "type": "integer",
                "description": "1-based inclusive end; omit for end of file.",
            },
        },
        "required": ["path"],
    }

    def run(self, parameters: Dict[str, Any]) -> str:

        try:

            path = safe_resolve_path(parameters["path"])
            start = parameters.get("start_line", 1)
            end = parameters.get("end_line")

            with open(path, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            total = len(lines)

            end = end or total

            content = "".join(lines[start - 1 : end])

            return self.success(
                {
                    "content": content,
                    "start_line": start,
                    "end_line": end,
                    "total_lines": total,
                }
            )

        except Exception as e:
            return self.fail(str(e))


# ==== 文件写入工具 ====


class WriteFileTool(BaseTool):
    """整文件覆盖写入。"""

    name = "write_file"
    description = "Overwrite file"

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path, or absolute if it resolves under the workspace; outside rejected.",
            },
            "content": {
                "type": "string",
                "description": "Full file UTF-8 body (overwrites existing).",
            },
        },
        "required": ["path", "content"],
    }

    def run(self, parameters: Dict[str, Any]) -> str:

        try:

            path = safe_resolve_path(parameters["path"])

            path.write_text(parameters["content"], encoding="utf-8")

            return self.success("written")

        except Exception as e:
            return self.fail(str(e))


# ==== 文件编辑工具 ====


class ReplaceInFileTool(BaseTool):
    """替换文件中的唯一文本块。"""

    name = "replace_in_file"
    description = "Safely replace one unique text block; preferred for multi-line block edits"

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path, or absolute if it resolves under the workspace; outside rejected.",
            },
            "old_string": {
                "type": "string",
                "description": "Exact current text to replace. Must match exactly once in the file and should include enough surrounding context to stay unique.",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text for old_string.",
            },
        },
        "required": ["path", "old_string", "new_string"],
    }

    def run(self, parameters: Dict[str, Any]) -> str:

        try:

            path = safe_resolve_path(parameters["path"])
            old_string = parameters["old_string"]
            new_string = parameters["new_string"]

            if not old_string:
                return self.fail("old_string must not be empty")

            content = path.read_text(encoding="utf-8")
            match_count = content.count(old_string)

            if match_count == 0:
                return self.fail("old_string not found")

            if match_count > 1:
                return self.fail(
                    f"old_string is not unique (found {match_count} matches)"
                )

            updated = content.replace(old_string, new_string, 1)
            path.write_text(updated, encoding="utf-8")

            return self.success(
                {
                    "path": to_workspace_relative(path),
                    "replacements": 1,
                }
            )

        except Exception as e:
            return self.fail(str(e))


class EditByLinesTool(BaseTool):
    """按行号替换指定区间内容。"""

    name = "edit_by_lines"
    description = "Replace one exact, already-verified line range; always pass old_text from read_file_lines"

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path, or absolute if it resolves under the workspace; outside rejected.",
            },
            "start_line": {
                "type": "integer",
                "description": "1-based inclusive start line of the exact range to replace.",
            },
            "end_line": {
                "type": "integer",
                "description": "1-based inclusive end line of the exact range to replace.",
            },
            "old_text": {
                "type": "string",
                "description": "Exact current text in start_line..end_line, copied from a fresh read_file_lines call. The tool refuses to edit if this does not match.",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text for exactly the selected line range. Do not include unchanged wrapper lines that are outside start_line..end_line.",
            },
        },
        "required": ["path", "start_line", "end_line", "old_text", "new_text"],
    }

    def run(self, parameters: Dict[str, Any]) -> str:

        try:

            path = safe_resolve_path(parameters["path"])
            start_line = parameters["start_line"]
            end_line = parameters["end_line"]
            old_text = parameters["old_text"]
            new_text = parameters["new_text"]

            if start_line < 1 or end_line < start_line:
                return self.fail("invalid line range")

            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            total_lines = len(lines)

            if end_line > total_lines:
                return self.fail(
                    f"line range out of bounds (file has {total_lines} lines)"
                )

            current_text = "".join(lines[start_line - 1 : end_line])
            normalized_old_text = old_text.replace("\r\n", "\n").replace("\r", "\n")
            normalized_current_text = current_text.replace("\r\n", "\n").replace(
                "\r", "\n"
            )
            if normalized_old_text != normalized_current_text:
                return self.fail(
                    "old_text does not match the current file content for the given line range; re-read the file and retry with the exact current content"
                )

            replacement_lines = new_text.splitlines(keepends=True)
            if new_text and not new_text.endswith(("\n", "\r")):
                replacement_lines.append("\n")

            updated_lines = (
                lines[: start_line - 1] + replacement_lines + lines[end_line:]
            )
            path.write_text("".join(updated_lines), encoding="utf-8")

            return self.success(
                {
                    "path": to_workspace_relative(path),
                    "start_line": start_line,
                    "end_line": end_line,
                    "new_line_count": len(replacement_lines),
                }
            )

        except Exception as e:
            return self.fail(str(e))


# ==== 命令执行工具 ====


def build_non_interactive_command_env() -> Dict[str, str]:
    """构造适合自动化执行命令的环境变量。"""
    env = os.environ.copy()
    env.setdefault("CI", "1")
    env.setdefault("TERM", "dumb")
    return env


def decode_subprocess_output(output: Any) -> str:
    """稳健解码子进程输出，优先兼容 UTF-8 并回退本地编码。"""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if not isinstance(output, (bytes, bytearray)):
        return str(output)

    raw = bytes(output)
    preferred_encoding = locale.getpreferredencoding(False) or "utf-8"
    encodings = ["utf-8", preferred_encoding, "gbk", "cp936"]
    tried = set()

    for encoding in encodings:
        normalized = (encoding or "").strip().lower()
        if not normalized or normalized in tried:
            continue
        tried.add(normalized)
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    return raw.decode("utf-8", errors="replace")


def split_background_command(command: str, background: bool) -> tuple[str, bool]:
    """兼容显式后台参数和命令末尾的 & 后台标记。"""
    normalized = command.rstrip()
    if normalized.endswith("&"):
        return normalized[:-1].rstrip(), True
    return command, background


def is_process_running(pid: int) -> bool:
    """跨平台判断进程是否仍在运行。"""
    if pid <= 0:
        return False

    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok and exit_code.value == still_active)

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_background_process(pid: int) -> tuple[bool, str]:
    """跨平台停止后台作业，对 Windows 采用树状终止。"""
    if pid <= 0:
        return False, "invalid pid"

    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=False,
            stdin=subprocess.DEVNULL,
        )
        output = "\n".join(
            part
            for part in (
                decode_subprocess_output(result.stdout).strip(),
                decode_subprocess_output(result.stderr).strip(),
            )
            if part
        )
        if result.returncode == 0:
            return True, output
        return False, output or f"taskkill failed with exit code {result.returncode}"

    try:
        os.killpg(pid, signal.SIGTERM)
        return True, ""
    except ProcessLookupError:
        return False, "process not found"
    except Exception as exc:
        return False, str(exc)


def read_log_tail(path: Path, tail_lines: int = 80) -> str:
    """读取日志文件尾部若干行，解码失败时自动兜底。"""
    if tail_lines < 1:
        tail_lines = 1
    if not path.exists():
        return ""

    try:
        content = decode_subprocess_output(path.read_bytes())
    except Exception:
        return ""
    content = content.replace("\r\n", "\n").replace("\r", "\n")

    lines = content.splitlines()
    if len(lines) <= tail_lines:
        return content.rstrip("\n")
    return "\n".join(lines[-tail_lines:])


def launch_background_command(
    background_job_store: BackgroundJobStore,
    *,
    command: str,
    cwd: Path,
    env: Dict[str, str],
) -> Dict[str, Any]:
    """启动后台命令并落盘后台作业与日志元数据。"""
    job_id = str(uuid.uuid4())[:8]
    stdout_log = background_job_store.log_dir / f"{job_id}.stdout.log"
    stderr_log = background_job_store.log_dir / f"{job_id}.stderr.log"
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stdout_log.touch(exist_ok=True)
    stderr_log.touch(exist_ok=True)
    stdout_handle = stdout_log.open("ab")
    stderr_handle = stderr_log.open("ab")
    popen_kwargs: Dict[str, Any] = {
        "shell": True,
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": stdout_handle,
        "stderr": stderr_handle,
        "env": env,
    }
    try:
        if os.name == "nt":
            creationflags = 0
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
            if creationflags:
                popen_kwargs["creationflags"] = creationflags
        else:
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(command, **popen_kwargs)
    finally:
        stdout_handle.close()
        stderr_handle.close()

    job = background_job_store.create_job(
        job_id=job_id,
        command=command,
        pid=process.pid,
        pid_role="launcher",
        cwd=cwd,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )
    return {
        "stdout": "",
        "stderr": "",
        "exit_code": None,
        "command": command,
        "background": True,
        "pid": process.pid,
        "pid_role": "launcher",
        "job_id": job["id"],
        "status": job["status"],
        "stdout_log": job["stdout_log"],
        "stderr_log": job["stderr_log"],
    }


def looks_like_service_ready_log(output: str) -> bool:
    """从常见开发服务器日志中粗略判断服务已就绪。"""
    if not output:
        return False
    lowered = output.lower()
    markers = (
        "http://",
        "https://",
        "localhost:",
        "127.0.0.1:",
        "ready in",
        "local:",
        "network:",
        "listening on",
    )
    return any(marker in lowered for marker in markers)


def is_tcp_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """检测 TCP 端口是否已可连接。"""
    if port < 1 or port > 65535:
        return False
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False

    for family, socktype, proto, _, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            return True
        except OSError:
            continue
        finally:
            sock.close()
    return False


def wait_for_background_service(
    background_job_store: BackgroundJobStore,
    *,
    job_id: str,
    startup_timeout: float,
    poll_interval: float,
    host: str,
    port: Optional[int],
    tail_lines: int,
) -> Dict[str, Any]:
    """等待后台服务达到可用状态，并在超时后有界返回。"""
    deadline = time.time() + startup_timeout
    attempts = 0
    latest_job: Optional[Dict[str, Any]] = None
    latest_stdout = ""
    latest_stderr = ""

    while True:
        attempts += 1
        latest_job = background_job_store.refresh_job(job_id)
        if latest_job is None:
            raise KeyError("background job not found")

        latest_stdout = read_log_tail(Path(latest_job["stdout_log"]), tail_lines)
        latest_stderr = read_log_tail(Path(latest_job["stderr_log"]), tail_lines)
        combined = "\n".join(part for part in (latest_stdout, latest_stderr) if part)

        if latest_job["status"] != "running":
            return {
                **latest_job,
                "ready": False,
                "timed_out": False,
                "attempts": attempts,
                "verification": "process_exited",
                "stdout": latest_stdout,
                "stderr": latest_stderr,
            }

        if port is not None and is_tcp_port_open(host, port):
            return {
                **latest_job,
                "ready": True,
                "timed_out": False,
                "attempts": attempts,
                "verification": "tcp_port",
                "host": host,
                "port": port,
                "url": f"http://{host}:{port}",
                "stdout": latest_stdout,
                "stderr": latest_stderr,
            }

        if looks_like_service_ready_log(combined):
            data: Dict[str, Any] = {
                **latest_job,
                "ready": True,
                "timed_out": False,
                "attempts": attempts,
                "verification": "log_output",
                "stdout": latest_stdout,
                "stderr": latest_stderr,
            }
            if port is not None:
                data["host"] = host
                data["port"] = port
                data["url"] = f"http://{host}:{port}"
            return data

        if time.time() >= deadline:
            return {
                **latest_job,
                "ready": False,
                "timed_out": True,
                "attempts": attempts,
                "verification": "timeout",
                "host": host,
                "port": port,
                "stdout": latest_stdout,
                "stderr": latest_stderr,
            }

        time.sleep(min(max(poll_interval, 0.1), 5.0))


def looks_like_interactive_prompt(output: str) -> bool:
    """根据命令输出粗略判断是否正在等待人工输入。"""
    if not output:
        return False

    normalized = output.strip()
    if not normalized:
        return False

    direct_markers = (
        "│  ○",
        "│  ●",
        "◆",
        "请选择",
        "是否继续",
        "press enter",
        "yes/no",
        "y/n",
    )
    lowered = normalized.lower()
    keyword_markers = (
        "select an option",
        "pick an option",
        "choose an option",
        "which template",
        "which variant",
        "confirm",
        "use vite",
        "use bun",
        "use typescript",
        "would you like",
    )

    if any(marker in normalized for marker in direct_markers):
        return True
    if any(marker in lowered for marker in keyword_markers):
        return True
    return re.search(r"(?m)^\s*[?？].+", normalized) is not None


class RunCommandTool(BaseTool):
    """在工作区内执行受限命令。"""

    def __init__(self, background_job_store: Optional[BackgroundJobStore] = None) -> None:
        self.background_job_store = background_job_store or BackgroundJobStore()

    name = "run_command"
    description = "Execute shell command"

    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command; cwd is workspace; dangerous patterns blocked.",
            },
            "timeout": {
                "type": "integer",
                "description": "Foreground timeout seconds (default 30); ignored when background.",
            },
            "background": {
                "type": "boolean",
                "description": "If true, run detached; returns job_id (default false).",
            },
        },
        "required": ["command"],
    }

    def run(self, parameters: Dict[str, Any]) -> str:

        command = parameters["command"]
        timeout = parameters.get("timeout", 30)
        background = parameters.get("background", False)

        deny = ["rm -rf", "shutdown", "reboot", "sudo"]

        if any(x in command for x in deny):
            return self.fail("command not allowed")

        try:
            env = build_non_interactive_command_env()
            command, background = split_background_command(command, background)

            if background:
                return self.success(
                    launch_background_command(
                        self.background_job_store,
                        command=command,
                        cwd=WORKSPACE_DIR,
                        env=env,
                    )
                )

            p = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=False,
                timeout=timeout,
                cwd=WORKSPACE_DIR,
                stdin=subprocess.DEVNULL,
                env=env,
            )

            return self.success(
                {
                    "stdout": decode_subprocess_output(p.stdout),
                    "stderr": decode_subprocess_output(p.stderr),
                    "exit_code": p.returncode,
                }
            )

        except subprocess.TimeoutExpired as exc:
            combined_output = "\n".join(
                part
                for part in (
                    decode_subprocess_output(exc.stdout),
                    decode_subprocess_output(exc.stderr),
                )
                if part
            )
            if looks_like_interactive_prompt(combined_output):
                return self.fail(
                    "command timed out and appears to be waiting for interactive input; "
                    "please rerun it with non-interactive flags such as --yes, -y, "
                    "--no-interactive, or explicit options"
                )
            return self.fail(f"command timed out after {timeout}s")
        except Exception as e:
            return self.fail(str(e))


class StartBackgroundServiceTool(BaseTool):
    """启动后台服务并等待端口或日志信号就绪。"""

    def __init__(self, background_job_store: Optional[BackgroundJobStore] = None) -> None:
        self.background_job_store = background_job_store or BackgroundJobStore()

    name = "start_background_service"
    description = "Start background service and wait for readiness"
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command; cwd is workspace; runs as background job.",
            },
            "port": {
                "type": "integer",
                "description": "TCP port to probe for readiness; omit for log-only wait.",
            },
            "host": {
                "type": "string",
                "description": "Host for port check (default localhost).",
            },
            "startup_timeout": {
                "type": "number",
                "description": "Max seconds to wait for ready (default 12).",
            },
            "poll_interval": {
                "type": "number",
                "description": "Seconds between readiness polls (default 1).",
            },
            "tail_lines": {
                "type": "integer",
                "description": "Log tail lines per poll (default 40).",
            },
        },
        "required": ["command"],
    }

    def run(self, parameters: Dict[str, Any]) -> str:
        command = parameters["command"]
        host = str(parameters.get("host", "localhost")).strip() or "localhost"
        port = parameters.get("port")
        startup_timeout = float(parameters.get("startup_timeout", 12))
        poll_interval = float(parameters.get("poll_interval", 1))
        tail_lines = int(parameters.get("tail_lines", 40))

        if port is not None:
            try:
                port = int(port)
            except (TypeError, ValueError):
                return self.fail("port must be an integer")
        if startup_timeout <= 0:
            return self.fail("startup_timeout must be > 0")
        if poll_interval <= 0:
            return self.fail("poll_interval must be > 0")
        if tail_lines < 1:
            return self.fail("tail_lines must be >= 1")

        deny = ["rm -rf", "shutdown", "reboot", "sudo"]
        if any(x in command for x in deny):
            return self.fail("command not allowed")

        try:
            env = build_non_interactive_command_env()
            command, _ = split_background_command(command, True)
            launch_result = launch_background_command(
                self.background_job_store,
                command=command,
                cwd=WORKSPACE_DIR,
                env=env,
            )
            ready_result = wait_for_background_service(
                self.background_job_store,
                job_id=str(launch_result["job_id"]),
                startup_timeout=startup_timeout,
                poll_interval=poll_interval,
                host=host,
                port=port,
                tail_lines=tail_lines,
            )
            return self.success(
                {
                    **launch_result,
                    **ready_result,
                }
            )
        except Exception as e:
            return self.fail(str(e))


class ListBackgroundJobsTool(BaseTool):
    """查询后台作业列表或单个作业状态。"""

    def __init__(self, background_job_store: Optional[BackgroundJobStore] = None) -> None:
        self.background_job_store = background_job_store or BackgroundJobStore()

    name = "list_background_jobs"
    description = "List or inspect background jobs"
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "If set, return that job; else list recent jobs.",
            },
            "limit": {
                "type": "integer",
                "description": "List cap when job_id omitted (default 10).",
            },
        },
    }

    def run(self, parameters: Dict[str, Any]) -> str:
        try:
            job_id = str(parameters.get("job_id", "")).strip()
            limit = parameters.get("limit", 10)
            if job_id:
                job = self.background_job_store.refresh_job(job_id)
                if job is None:
                    return self.fail("background job not found")
                return self.success(job)

            if limit is not None and int(limit) < 1:
                return self.fail("limit must be >= 1")
            jobs = self.background_job_store.refresh_jobs(limit=int(limit))
            return self.success(jobs)
        except Exception as e:
            return self.fail(str(e))


class ReadBackgroundJobLogTool(BaseTool):
    """读取后台作业日志。"""

    def __init__(self, background_job_store: Optional[BackgroundJobStore] = None) -> None:
        self.background_job_store = background_job_store or BackgroundJobStore()

    name = "read_background_job_log"
    description = "Read background job logs"
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "Background job id from run_command/start_background_service.",
            },
            "stream": {
                "type": "string",
                "enum": ["stdout", "stderr", "both"],
                "description": "Which log(s) to return (default both).",
            },
            "tail_lines": {
                "type": "integer",
                "description": "Lines to read from each selected stream tail (default 80).",
            },
        },
        "required": ["job_id"],
    }

    def run(self, parameters: Dict[str, Any]) -> str:
        try:
            job_id = str(parameters["job_id"]).strip()
            stream = str(parameters.get("stream", "both")).strip() or "both"
            tail_lines = int(parameters.get("tail_lines", 80))
            job = self.background_job_store.refresh_job(job_id)
            if job is None:
                return self.fail("background job not found")

            stdout_text = ""
            stderr_text = ""
            if stream in {"stdout", "both"}:
                stdout_text = read_log_tail(Path(job["stdout_log"]), tail_lines)
            if stream in {"stderr", "both"}:
                stderr_text = read_log_tail(Path(job["stderr_log"]), tail_lines)

            return self.success(
                {
                    "job_id": job_id,
                    "status": job["status"],
                    "stream": stream,
                    "tail_lines": tail_lines,
                    "stdout_log": job["stdout_log"],
                    "stderr_log": job["stderr_log"],
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                }
            )
        except Exception as e:
            return self.fail(str(e))


class StopBackgroundJobTool(BaseTool):
    """停止后台作业。"""

    def __init__(self, background_job_store: Optional[BackgroundJobStore] = None) -> None:
        self.background_job_store = background_job_store or BackgroundJobStore()

    name = "stop_background_job"
    description = "Stop a background job"
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "Running background job id to stop.",
            },
        },
        "required": ["job_id"],
    }

    def run(self, parameters: Dict[str, Any]) -> str:
        try:
            job_id = str(parameters["job_id"]).strip()
            job = self.background_job_store.refresh_job(job_id)
            if job is None:
                return self.fail("background job not found")
            if job["status"] != "running":
                return self.success(
                    {
                        "job_id": job_id,
                        "pid": job["pid"],
                        "status": job["status"],
                        "stopped": False,
                    }
                )

            ok, details = stop_background_process(int(job["pid"]))
            if not ok:
                if not is_process_running(int(job["pid"])):
                    updated = self.background_job_store.update_status(
                        job_id,
                        "stopped",
                        stopped_at=time.time(),
                    )
                    return self.success(
                        {
                            "job_id": job_id,
                            "pid": updated["pid"],
                            "status": updated["status"],
                            "stopped": True,
                            "details": details,
                        }
                    )
                return self.fail(details or "failed to stop background job")

            updated = self.background_job_store.update_status(
                job_id,
                "stopped",
                stopped_at=time.time(),
            )
            return self.success(
                {
                    "job_id": job_id,
                    "pid": updated["pid"],
                    "status": updated["status"],
                    "stopped": True,
                    "details": details,
                }
            )
        except Exception as e:
            return self.fail(str(e))


class SleepTool(BaseTool):
    """提供跨平台等待能力，避免依赖 shell 睡眠命令。"""

    name = "sleep"
    description = "Sleep for a few seconds"
    parameters = {
        "type": "object",
        "properties": {
            "seconds": {
                "type": "number",
                "description": "Sleep duration 0–30s; avoids shell sleep in tools.",
            },
        },
        "required": ["seconds"],
    }

    def run(self, parameters: Dict[str, Any]) -> str:
        try:
            seconds = float(parameters["seconds"])
        except (TypeError, ValueError, KeyError):
            return self.fail("seconds must be a number")

        if seconds < 0:
            return self.fail("seconds must be >= 0")
        if seconds > 30:
            return self.fail("seconds must be <= 30")

        time.sleep(seconds)
        return self.success({"slept_seconds": seconds})


class BaseAgent:
    """封装带工具调用能力的基础 Agent。"""
    def __init__(
        self,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        agent_name: str = "助手",
    ):
        self.model = model or OPENAI_MODEL
        self.agent_name = agent_name
        self.agent_color = INFO_COLOR
        self.client = OpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL,
            timeout=300.0,
        )
        self.tools: List[BaseTool] = []
        self.system_prompt = system_prompt or "You are a helpful assistant."
        self.base_messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": self.system_prompt,
            }
        ]
        self.messages: List[Dict[str, Any]] = list(self.base_messages)
        self.history_messages: List[Dict[str, Any]] = list(self.base_messages)
        self.latest_usage: Optional[UsageSnapshot] = None
        self.latest_turn_metrics: Optional[TurnMetrics] = None

    def register_tool(self, tool: BaseTool) -> None:
        self.tools.append(tool)

    def get_tools(self) -> List[Dict[str, Any]]:
        return [{"type": "function", "function": tool.to_dict()} for tool in self.tools]

    def get_context_window(self) -> Optional[int]:
        """返回当前模型的上下文窗口大小。"""
        return resolve_model_context_window(self.model)

    def get_usage_snapshot(self) -> Optional[UsageSnapshot]:
        """返回最近一次流式请求记录到的 usage。"""
        return self.latest_usage

    def get_latest_turn_metrics(self) -> Optional[TurnMetrics]:
        """返回最近一次完整对话回合的性能统计。"""
        return self.latest_turn_metrics

    def get_usage_report_lines(self) -> List[str]:
        """生成 usage 报告文本。"""
        usage = self.get_usage_snapshot()
        if usage is None:
            return [f"当前还没有 {self.agent_name} 的 usage 数据，请先完成至少一次对话。"]

        return [self.build_compact_context_usage_text(usage)]

    def get_turn_report_lines(self) -> List[str]:
        """生成最近一轮对话的速度与 usage 统计。"""
        metrics = self.get_latest_turn_metrics()
        if metrics is None:
            return [f"当前还没有 {self.agent_name} 的回合统计，请先完成至少一次对话。"]

        lines = [
            self.build_compact_turn_summary(metrics)
        ]
        return lines

    def print_turn_report(self) -> None:
        """把最近一轮统计打印到界面。"""
        metrics = self.get_latest_turn_metrics()
        if metrics is None:
            return
        print_soft_line(
            f"{self.agent_name}  ",
            self.build_compact_turn_summary(metrics),
            self.agent_color,
        )

    def build_compact_context_usage_text(self, usage: UsageSnapshot) -> str:
        """构造简洁的上下文占用文本。"""
        context_limit = self.get_context_window()
        return (
            "ctx "
            f"{format_token_count(usage.prompt_tokens)}"
            f"/{format_token_count(context_limit) if context_limit else '未知'} "
            f"({format_percent(usage.prompt_tokens, context_limit)})"
        )

    def build_compact_turn_summary(self, metrics: TurnMetrics) -> str:
        """构造简洁的单行回合统计。"""
        parts = [
            format_token_speed(
                metrics.cumulative_completion_tokens,
                metrics.generation_seconds,
            )
        ]
        if metrics.final_usage is not None:
            parts.append(self.build_compact_context_usage_text(metrics.final_usage))
        return "  ·  ".join(parts)

    def _build_usage_snapshot(self, raw_usage: Dict[str, Any]) -> Optional[UsageSnapshot]:
        """把 usage 字典转换为标准快照。"""
        if not raw_usage:
            return None
        return UsageSnapshot(
            prompt_tokens=self._int_from_usage(raw_usage, "prompt_tokens"),
            completion_tokens=self._int_from_usage(raw_usage, "completion_tokens"),
            total_tokens=self._int_from_usage(raw_usage, "total_tokens"),
            updated_at=time.time(),
            raw=raw_usage,
        )

    def _usage_to_dict(self, usage: Any) -> Dict[str, Any]:
        """尽量把 SDK usage 对象转成普通字典。"""
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
        """安全读取 usage 中的整数值。"""
        value = raw_usage.get(key, 0)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def update_usage_snapshot(self, usage: Any) -> None:
        """从流式 chunk 中提取 usage，并覆盖最近一次统计。"""
        raw_usage = self._usage_to_dict(usage)
        snapshot = self._build_usage_snapshot(raw_usage)
        if snapshot is None:
            return
        self.latest_usage = snapshot

    def _clone_usage_snapshot(
        self, usage: Optional[UsageSnapshot]
    ) -> Optional[UsageSnapshot]:
        """复制 usage 快照，避免后续覆盖污染当前回合统计。"""
        if usage is None:
            return None
        return UsageSnapshot(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            updated_at=usage.updated_at,
            raw=json.loads(json.dumps(usage.raw, ensure_ascii=False)),
        )

    def finalize_turn_metrics(
        self,
        *,
        started_at: float,
        first_output_at: Optional[float],
        request_count: int,
        cumulative_prompt_tokens: int,
        cumulative_completion_tokens: int,
        cumulative_total_tokens: int,
    ) -> None:
        """收口一次完整 chat 调用的性能统计。"""
        self.latest_turn_metrics = TurnMetrics(
            agent_name=self.agent_name,
            model=self.model,
            started_at=started_at,
            finished_at=time.time(),
            first_output_at=first_output_at,
            request_count=request_count,
            cumulative_prompt_tokens=cumulative_prompt_tokens,
            cumulative_completion_tokens=cumulative_completion_tokens,
            cumulative_total_tokens=cumulative_total_tokens,
            final_usage=self._clone_usage_snapshot(self.latest_usage),
        )

    def execute_tool(self, name: str, args_json: str) -> str:
        try:
            args = json.loads(args_json)
        except json.JSONDecodeError:
            return "参数 JSON 解析失败"
        tool = next((t for t in self.tools if t.name == name), None)
        if not tool:
            return f"未找到工具：{name}"
        return tool.run(args)

    def format_tool_result(self, result: str, max_len: int = 600) -> str:
        try:
            payload = json.loads(result)
        except Exception:
            text = result.strip()
        else:
            if isinstance(payload, dict) and "success" in payload:
                if payload.get("success"):
                    text = json.dumps(
                        {"success": True, "data": payload.get("data")},
                        ensure_ascii=False,
                    )
                else:
                    text = json.dumps(
                        {"success": False, "error": payload.get("error")},
                        ensure_ascii=False,
                    )
            else:
                text = json.dumps(payload, ensure_ascii=False)

        text = text.strip()
        if not text:
            return "<empty>"
        if len(text) <= max_len:
            return text
        return text[:max_len] + "...<truncated>"

    def _coerce_stream_text(self, value: Any) -> str:
        """将 SDK 流式字段尽量规整为可直接打印的文本。"""
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
        """优先提取 reasoning_content；不写入模型上下文，但会记录到历史导出。"""
        for attr in ("reasoning_content", "reasoning"):
            text = self._coerce_stream_text(getattr(delta, attr, None))
            if text:
                return text
        return ""

    def reset_conversation(self) -> None:
        """将当前会话恢复到仅含 system prompt 的初始状态。"""
        self.messages = list(self.base_messages)
        self.history_messages = list(self.base_messages)
        self.latest_usage = None
        self.latest_turn_metrics = None

    def chat(
        self,
        message: str,
        *,
        silent: bool = False,
        reset_history: bool = False,
        stop_after_tool_names: Optional[List[str]] = None,
    ) -> str:
        """
        silent: 为 True 时不向用户打印任何内容（用于 exec_agent 内部执行，反馈给 plan_agent）
        """
        if reset_history:
            self.reset_conversation()

        stop_after_tool_names = set(stop_after_tool_names or [])
        self.latest_turn_metrics = None
        user_message = {"role": "user", "content": message}
        self.messages.append(user_message)
        self.history_messages.append(dict(user_message))
        tools = self.get_tools()
        turn_started_at = time.time()
        turn_first_output_at: Optional[float] = None
        turn_request_count = 0
        turn_prompt_tokens = 0
        turn_completion_tokens = 0
        turn_total_tokens = 0
        api_kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
            "stream": True,
            "max_tokens": 16384,
            "stream_options": {"include_usage": True, "continuous_usage_stats": True},
        }
        extra_body: Dict[str, Any] = {}
        
        if OPENAI_MODEL == "qwen/qwen3.5-35b-a3b":
            # 编码任务官方推荐参数（按思考模式区分）
            # Thinking coding: temp=0.6, top_p=0.95, top_k=20, min_p=0, presence_penalty=0
            # Non-thinking reasoning: temp=1.0, top_p=1.0, top_k=40, min_p=0, presence_penalty=2.0
            _PARAMS_CODING_THINKING = (0.6, 0.95, 20, 0, 0)
            _PARAMS_CODING_NON_THINKING = (1.0, 1.0, 40, 0, 2.0)
            if not OPENAI_ENABLE_THINKING:
                temp, top_p, top_k, min_p, presence_penalty = _PARAMS_CODING_NON_THINKING
                api_kwargs["temperature"] = temp
                api_kwargs["top_p"] = top_p
                api_kwargs["presence_penalty"] = presence_penalty
                
                extra_body["reasoning"] = {"enabled": False}
                extra_body["top_k"] = top_k
                extra_body["min_p"] = min_p
            else:
                temp, top_p, top_k, min_p, presence_penalty = _PARAMS_CODING_THINKING
                api_kwargs["temperature"] = temp
                api_kwargs["top_p"] = top_p
                api_kwargs["presence_penalty"] = presence_penalty
                
                extra_body["reasoning"] = {"enabled": True}
                extra_body["top_k"] = top_k
                extra_body["min_p"] = min_p
        else:
            if not OPENAI_ENABLE_THINKING:
                extra_body["chat_template_kwargs"] = {"enable_thinking": False}
            else:
                extra_body["chat_template_kwargs"] = {"enable_thinking": True}
        
        api_kwargs["extra_body"] = extra_body
        
        if tools:
            api_kwargs["tools"] = tools
            api_kwargs["tool_choice"] = "auto"

        while True:
            turn_request_count += 1
            stream = self.client.chat.completions.create(**api_kwargs)

            content_parts: List[str] = []
            reasoning_parts: List[str] = []
            tool_call_acc: Dict[str, Dict[str, str]] = {}
            last_tool_call_id: Optional[str] = None
            request_usage_snapshot: Optional[UsageSnapshot] = None

            if not silent:
                print(
                    f"\n{color_text(f'{self.agent_name}：', self.agent_color)}",
                    end="",
                    flush=True,
                )
            tool_call_started = False  # 是否已输出过工具调用前缀
            reasoning_started = False
            answer_started = False
            for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    self.update_usage_snapshot(usage)
                    request_usage_snapshot = self._clone_usage_snapshot(self.latest_usage)

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                logger.info(delta)

                reasoning_text = self.get_reasoning_delta_text(delta)
                if reasoning_text and turn_first_output_at is None:
                    turn_first_output_at = time.time()
                if reasoning_text:
                    reasoning_parts.append(reasoning_text)
                if reasoning_text and not silent:
                    if not reasoning_started:
                        print(
                            "\n"
                            + color_text("【思考】", REASONING_COLOR)
                            + " ",
                            end="",
                            flush=True,
                        )
                        reasoning_started = True
                    print(
                        color_text(reasoning_text, REASONING_COLOR),
                        end="",
                        flush=True,
                    )

                if hasattr(delta, "content") and delta.content:
                    if turn_first_output_at is None:
                        turn_first_output_at = time.time()
                    content_parts.append(delta.content)
                    if not silent:
                        if reasoning_started and not answer_started:
                            print(
                                "\n"
                                + color_text("【回答】", self.agent_color)
                                + " ",
                                end="",
                                flush=True,
                            )
                            answer_started = True
                        print(delta.content, end="", flush=True)

                if hasattr(delta, "tool_calls") and delta.tool_calls:
                    if turn_first_output_at is None:
                        turn_first_output_at = time.time()
                    for tc in delta.tool_calls:
                        tc_id = tc.id or last_tool_call_id
                        if tc_id is None:
                            continue
                        last_tool_call_id = tc_id
                        if tc_id not in tool_call_acc:
                            tool_call_acc[tc_id] = {
                                "id": tc_id,
                                "name": "",
                                "arguments": "",
                            }
                            if not silent:
                                if reasoning_started and not answer_started:
                                    print("\n", end="", flush=True)
                                    answer_started = True
                                if not tool_call_started:
                                    print("【工具调用】", end="", flush=True)
                                    tool_call_started = True
                                else:
                                    print("\n【工具调用】", end="", flush=True)
                        if tc.function:
                            if tc.function.name:
                                tool_call_acc[tc_id]["name"] += tc.function.name
                                if not silent:
                                    print(tc.function.name, end="", flush=True)
                            if tc.function.arguments:
                                tool_call_acc[tc_id][
                                    "arguments"
                                ] += tc.function.arguments
                                if not silent:
                                    print(tc.function.arguments, end="", flush=True)

            if request_usage_snapshot is not None:
                turn_prompt_tokens += request_usage_snapshot.prompt_tokens
                turn_completion_tokens += request_usage_snapshot.completion_tokens
                turn_total_tokens += request_usage_snapshot.total_tokens

            full_content = "".join(content_parts)
            full_reasoning = "".join(reasoning_parts)

            if tool_call_acc:
                if not silent:
                    print()  # 工具调用流式输出后换行
                tool_calls_list = [
                    {
                        "id": data["id"],
                        "type": "function",
                        "function": {
                            "name": data["name"],
                            "arguments": data["arguments"],
                        },
                    }
                    for data in tool_call_acc.values()
                ]
                assistant_message = {
                    "role": "assistant",
                    "content": full_content or "",
                    "tool_calls": tool_calls_list,
                }
                self.messages.append(assistant_message)
                history_assistant_message = dict(assistant_message)
                if full_reasoning:
                    history_assistant_message["reasoning_content"] = full_reasoning
                self.history_messages.append(history_assistant_message)
                for call in tool_calls_list:
                    result = self.execute_tool(
                        call["function"]["name"],
                        call["function"]["arguments"],
                    )
                    if not silent:
                        print(
                            f"【工具结果】{call['function']['name']} -> "
                            f"{self.format_tool_result(result)}",
                            flush=True,
                        )
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": result,
                    }
                    self.messages.append(tool_message)
                    self.history_messages.append(dict(tool_message))
                if any(
                    call["function"]["name"] in stop_after_tool_names
                    for call in tool_calls_list
                ):
                    if not silent:
                        print()
                    self.finalize_turn_metrics(
                        started_at=turn_started_at,
                        first_output_at=turn_first_output_at,
                        request_count=turn_request_count,
                        cumulative_prompt_tokens=turn_prompt_tokens,
                        cumulative_completion_tokens=turn_completion_tokens,
                        cumulative_total_tokens=turn_total_tokens,
                    )
                    if not silent:
                        self.print_turn_report()
                    return full_content
                continue

            if full_content:
                assistant_message = {"role": "assistant", "content": full_content}
                self.messages.append(assistant_message)
                history_assistant_message = dict(assistant_message)
                if full_reasoning:
                    history_assistant_message["reasoning_content"] = full_reasoning
                self.history_messages.append(history_assistant_message)
                if not silent:
                    print()  # 流式输出后换行
                self.finalize_turn_metrics(
                    started_at=turn_started_at,
                    first_output_at=turn_first_output_at,
                    request_count=turn_request_count,
                    cumulative_prompt_tokens=turn_prompt_tokens,
                    cumulative_completion_tokens=turn_completion_tokens,
                    cumulative_total_tokens=turn_total_tokens,
                )
                if not silent:
                    self.print_turn_report()
                return full_content

            # 空响应时避免死循环
            logger.warning("API 返回空响应")
            self.finalize_turn_metrics(
                started_at=turn_started_at,
                first_output_at=turn_first_output_at,
                request_count=turn_request_count,
                cumulative_prompt_tokens=turn_prompt_tokens,
                cumulative_completion_tokens=turn_completion_tokens,
                cumulative_total_tokens=turn_total_tokens,
            )
            if not silent:
                self.print_turn_report()
            return ""


# ==== Plan Agent ====

TASK_STATUS = ["pending", "running", "done", "failed"]


class TaskPlanTool(BaseTool):
    """向任务存储写入规划后的任务列表。"""
    def __init__(
        self,
        task_store: TaskStore,
        session_id_provider: Optional[Callable[[], Optional[str]]] = None,
        request_input_provider: Optional[Callable[[], Optional[str]]] = None,
    ):
        self.task_store = task_store
        self.session_id_provider = session_id_provider
        self.request_input_provider = request_input_provider

    name = "task_plan"
    description = "Create tasks"

    parameters = {
        "type": "object",
        "properties": {
            "request_summary": {
                "type": "string",
                "description": "Short label for this user goal (one request).",
            },
            "tasks": {
                "type": "array",
                "description": "Ordered steps; executor runs them via execute_next_task.",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "One-line actionable task text.",
                        }
                    },
                    "required": ["description"],
                },
            },
        },
        "required": ["request_summary", "tasks"],
    }

    def run(self, parameters: Dict[str, Any]) -> str:
        session_id = (
            self.session_id_provider() if callable(self.session_id_provider) else None
        )
        user_input = (
            self.request_input_provider() if callable(self.request_input_provider) else None
        )
        active_request = self.task_store.get_active_request(session_id)
        if active_request is not None:
            return self.fail(
                "active request exists; continue executing current request before creating a new task plan"
            )
        created = self.task_store.create_tasks(
            parameters["tasks"],
            session_id=session_id,
            request_summary=str(parameters.get("request_summary", "")).strip(),
            user_input=user_input,
        )
        return self.success(created)


class TaskUpdateTool(BaseTool):
    """更新任务执行状态和结果。"""
    def __init__(
        self,
        task_store: TaskStore,
        result_enricher: Optional[Callable[[str, str, Optional[str]], Optional[str]]] = None,
    ):
        self.task_store = task_store
        self.result_enricher = result_enricher

    name = "update_task"
    description = "Set task status and optional result text"

    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task id from task_plan or read_tasks.",
            },
            "status": {
                "type": "string",
                "enum": ["pending", "running", "done", "failed"],
                "description": "Lifecycle state to record.",
            },
            "result": {
                "type": "string",
                "description": "Outcome summary for done/failed; optional otherwise.",
            },
        },
        "required": ["task_id", "status"],
    }

    def run(self, parameters: Dict[str, Any]) -> str:

        try:
            result = parameters.get("result")
            if callable(self.result_enricher):
                result = self.result_enricher(
                    parameters["task_id"],
                    parameters["status"],
                    result,
                )
            updated = self.task_store.update_task(
                task_id=parameters["task_id"],
                status=parameters["status"],
                result=result,
            )
            return self.success(updated)
        except KeyError:
            return self.fail("task not found")
        except ValueError:
            return self.fail("invalid status")


class ReadTasksTool(BaseTool):
    """按当前会话读取任务信息。"""

    def __init__(
        self,
        task_store: TaskStore,
        session_id_provider: Optional[Callable[[], Optional[str]]] = None,
    ):
        self.task_store = task_store
        self.session_id_provider = session_id_provider

    name = "read_tasks"
    description = "Read current session tasks"

    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "If set, return one task; omit for all requests in session.",
            },
        },
    }

    def run(self, parameters: Dict[str, Any]) -> str:
        session_id = (
            self.session_id_provider() if callable(self.session_id_provider) else None
        )
        task_id = parameters.get("task_id")

        if task_id:
            task = self.task_store.get(task_id)
            if task is None or task.session_id != session_id:
                return self.fail("task not found")
            task_data = self.task_store.get_task_dict(task.id)
            if task_data is None:
                return self.fail("task not found")
            return self.success(task_data)

        return self.success(self.task_store.list_requests(session_id=session_id))


def execute_single_task(
    exec_agent: "ExecuteAgent",
    task_store: TaskStore,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """取出一个待执行任务，并交给 ExecuteAgent 处理。"""
    task = task_store.get_next_pending(session_id=session_id)
    if task is None:
        return {"executed": False, "task": None}

    task_store.update_task(task.id, "running")
    print(color_text(f"\n[执行中] {task.description}", EXECUTE_COLOR))
    request = task_store.get_request(task.request_id or "")
    request_summary = request.summary.strip() if request and request.summary else "未记录"
    request_user_input = request.user_input.strip() if request and request.user_input else ""

    previous_task_lines: List[str] = []
    for previous in task_store.completed_tasks(
        session_id=session_id,
        request_id=task.request_id,
    ):
        result = (previous.get("result") or "").strip()
        if len(result) > 200:
            result = result[:200] + "..."
        previous_task_lines.append(
            f"- [{previous['status']}] {previous['description']}"
            + (f" | 结果：{result}" if result else "")
        )

    previous_task_summary = "\n".join(previous_task_lines) or "无"

    task_prompt = build_execute_task_prompt_xml(
        task_id=task.id,
        request_id=task.request_id or "未记录",
        request_summary=request_summary,
        request_user_input=request_user_input,
        task_description=task.description,
        previous_task_summary=previous_task_summary,
    )

    exec_agent.active_session_id = session_id
    exec_agent.active_task_id = task.id
    exec_agent.recent_background_jobs = []
    try:
        result = exec_agent.chat(
            task_prompt,
            silent=False,
            reset_history=False,
            stop_after_tool_names=["update_task"],
        )
    except Exception as e:
        logger.exception("执行任务失败: %s", task.description)
        result = f"执行异常：{e}"
        task_store.update_task(task.id, "failed", result=result)
    finally:
        exec_agent.active_session_id = None
        exec_agent.active_task_id = None
        exec_agent.recent_background_jobs = []

    latest_task = task_store.get(task.id)
    if latest_task and latest_task.status == "running":
        task_store.update_task(task.id, "done", result=result)
        latest_task = task_store.get(task.id)
    elif latest_task and not latest_task.result:
        task_store.update_task(task.id, latest_task.status, result=result)
        latest_task = task_store.get(task.id)

    if latest_task is None:
        raise RuntimeError(f"task disappeared: {task.id}")

    print(
        color_text(
            f"[任务结束] {latest_task.description} -> {latest_task.status}",
            EXECUTE_COLOR,
        )
    )
    latest_task_data = task_store.get_task_dict(task.id)
    return {"executed": True, "task": latest_task_data}


class PlanAgent(BaseAgent):
    """负责理解需求、拆解任务并驱动执行流程。"""
    """
    1. 与用户直接交互的 PlanAgent， 用户不会直接与 ExecuteAgent 交互
    2. 理解用户需求，使用工具查看环境、项目等结构。做出规划。 并生成任务列表。
    3. 分配任务给 ExecuteAgent 执行。
    4. ExecuteAgent 执行任务，直到子任务完成。并反馈任务进度。
    5. 当子任务完成时，PlanAgent 主动汇报任务完成情况。
    6. 当所有子任务完成时，PlanAgent 主动汇报任务完成情况。
    """

    def __init__(
        self,
        task_store: TaskStore,
        background_job_store: Optional[BackgroundJobStore] = None,
        history_store: Optional[PlanHistoryStore] = None,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ):
        effective_model = model or PLAN_MODEL
        system_prompt = system_prompt or with_runtime_context(
            PLAN_AGENT_SYSTEM_PROMPT,
            agent_name="PlanAgent",
            model_name=effective_model,
            execution_mode="plan",
        )
        super().__init__(effective_model, system_prompt, agent_name="PlanAgent")
        self.agent_color = PLAN_COLOR
        self.task_store = task_store
        self.background_job_store = background_job_store or BackgroundJobStore()
        self.history_store = history_store or PlanHistoryStore()
        self.current_user_request_input: Optional[str] = None
        self.current_session_id = self.history_store.start_session(
            self.agent_name,
            self.history_messages,
        )
        self.register_tool(ListFilesTool())
        self.register_tool(SearchCodeTool())
        self.register_tool(ReadFileLinesTool())
        self.register_tool(ListBackgroundJobsTool(self.background_job_store))
        self.register_tool(ReadBackgroundJobLogTool(self.background_job_store))
        self.register_tool(
            TaskPlanTool(
                task_store,
                session_id_provider=lambda: self.current_session_id,
                request_input_provider=lambda: self.current_user_request_input,
            )
        )

    def reset_conversation(self) -> None:
        """重置上下文，并为 PlanAgent 开启新的历史会话。"""
        if hasattr(self, "current_session_id"):
            self.history_store.sync_session(self.current_session_id, self.history_messages)
        super().reset_conversation()
        self.current_session_id = self.history_store.start_session(
            self.agent_name,
            self.history_messages,
        )

    def chat(
        self,
        message: str,
        *,
        silent: bool = False,
        reset_history: bool = False,
        stop_after_tool_names: Optional[List[str]] = None,
    ) -> str:
        self.current_user_request_input = message
        try:
            return super().chat(
                message,
                silent=silent,
                reset_history=reset_history,
                stop_after_tool_names=stop_after_tool_names,
            )
        finally:
            self.history_store.sync_session(
                self.current_session_id,
                self.history_messages,
            )
            self.current_user_request_input = None

    def export_history_markdown(self, output_path: Path, *, export_all: bool = False) -> Path:
        """导出当前对话或全部历史记录为 Markdown 文档。"""
        self.history_store.sync_session(
            self.current_session_id,
            self.history_messages,
        )
        self.history_store.export_markdown(
            output_path,
            current_session_id=self.current_session_id,
            only_session_id=None if export_all else self.current_session_id,
        )
        return output_path


# ==== Execute Agent ====


class ExecuteAgent(BaseAgent):
    """负责消费单个任务并落地执行。"""
    def __init__(
        self,
        task_store: TaskStore,
        background_job_store: Optional[BackgroundJobStore] = None,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ):
        effective_model = model or EXEC_MODEL
        system_prompt = system_prompt or with_runtime_context(
            EXECUTE_AGENT_SYSTEM_PROMPT,
            agent_name="ExecuteAgent",
            model_name=effective_model,
            execution_mode="execute",
        )
        super().__init__(effective_model, system_prompt, agent_name="ExecuteAgent")
        self.agent_color = EXECUTE_COLOR
        self.task_store = task_store
        self.background_job_store = background_job_store or BackgroundJobStore()
        self.active_session_id: Optional[str] = None
        self.active_task_id: Optional[str] = None
        self.recent_background_jobs: List[Dict[str, Any]] = []
        self.register_tool(ListFilesTool())
        self.register_tool(SearchCodeTool())
        self.register_tool(ReadFileLinesTool())
        self.register_tool(WriteFileTool())
        self.register_tool(ReplaceInFileTool())
        self.register_tool(EditByLinesTool())
        self.register_tool(RunCommandTool(self.background_job_store))
        self.register_tool(StartBackgroundServiceTool(self.background_job_store))
        self.register_tool(SleepTool())
        self.register_tool(ListBackgroundJobsTool(self.background_job_store))
        self.register_tool(ReadBackgroundJobLogTool(self.background_job_store))
        self.register_tool(StopBackgroundJobTool(self.background_job_store))
        self.register_tool(
            ReadTasksTool(
                task_store,
                session_id_provider=lambda: self.active_session_id,
            )
        )
        self.register_tool(
            TaskUpdateTool(
                task_store,
                result_enricher=self.enrich_task_result_with_background_jobs,
            )
        )

    def reset_conversation(self) -> None:
        """重置上下文与当前任务运行期状态。"""
        super().reset_conversation()
        self.active_session_id = None
        self.active_task_id = None
        self.recent_background_jobs = []

    def execute_tool(self, name: str, args_json: str) -> str:
        result = super().execute_tool(name, args_json)
        if name in {"run_command", "start_background_service"}:
            self.record_background_job_from_tool_result(result)
        elif name == "stop_background_job":
            self.sync_recent_background_jobs()
        return result

    def record_background_job_from_tool_result(self, result: str) -> None:
        """记录本任务内新启动的后台作业，供任务摘要自动补全。"""
        try:
            payload = json.loads(result)
        except Exception:
            return
        if not isinstance(payload, dict) or not payload.get("success"):
            return

        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("background"):
            return

        job_id = str(data.get("job_id", "")).strip()
        if not job_id:
            return
        if any(str(job.get("id")) == job_id for job in self.recent_background_jobs):
            return

        self.recent_background_jobs.append(
            {
                "id": job_id,
                "pid": data.get("pid"),
                "pid_role": data.get("pid_role", "launcher"),
                "status": data.get("status", "running"),
                "stdout_log": data.get("stdout_log", ""),
                "stderr_log": data.get("stderr_log", ""),
                "command": data.get("command", ""),
            }
        )

    def sync_recent_background_jobs(self) -> None:
        """把缓存中的后台作业状态刷新为最新值。"""
        refreshed_jobs: List[Dict[str, Any]] = []
        for job in self.recent_background_jobs:
            job_id = str(job.get("id", "")).strip()
            if not job_id:
                continue
            latest = self.background_job_store.refresh_job(job_id)
            if latest is None:
                refreshed_jobs.append(dict(job))
                continue
            refreshed_jobs.append(
                {
                    "id": latest.get("id", job_id),
                    "pid": latest.get("pid", job.get("pid")),
                    "pid_role": latest.get("pid_role", job.get("pid_role", "launcher")),
                    "status": latest.get("status", job.get("status", "running")),
                    "stdout_log": latest.get("stdout_log", job.get("stdout_log", "")),
                    "stderr_log": latest.get("stderr_log", job.get("stderr_log", "")),
                    "command": latest.get("command", job.get("command", "")),
                }
            )
        self.recent_background_jobs = refreshed_jobs

    def enrich_task_result_with_background_jobs(
        self,
        task_id: str,
        status: str,
        result: Optional[str],
    ) -> Optional[str]:
        """把当前任务里启动的后台作业摘要补入 update_task 结果。"""
        if task_id != self.active_task_id or not self.recent_background_jobs:
            return result

        self.sync_recent_background_jobs()
        background_summary = build_background_job_result_summary(
            self.recent_background_jobs
        )
        if not background_summary:
            return result

        base = (result or "").strip()
        if base and "job_id=" in base:
            return base
        if base:
            return f"{base}\n{background_summary}"
        return background_summary


# ==== 任务分发工具 ====


class ExecuteNextTaskTool(BaseTool):
    """把下一个待办任务分发给 ExecuteAgent。"""
    def __init__(
        self,
        task_store: TaskStore,
        exec_agent: ExecuteAgent,
        session_id_provider: Optional[Callable[[], Optional[str]]] = None,
    ):
        self.task_store = task_store
        self.exec_agent = exec_agent
        self.session_id_provider = session_id_provider

    name = "execute_next_task"
    description = "Dispatch next pending task to ExecuteAgent"
    parameters = {
        "type": "object",
        "description": "No arguments; runs next pending task in current session.",
        "properties": {},
    }

    def run(self, parameters: Dict[str, Any]) -> str:
        try:
            session_id = (
                self.session_id_provider() if callable(self.session_id_provider) else None
            )
            result = execute_single_task(
                self.exec_agent,
                self.task_store,
                session_id=session_id,
            )
            return self.success(result)
        except Exception as e:
            return self.fail(str(e))


def print_task_summary(task_store: TaskStore, session_id: Optional[str] = None) -> None:
    """打印当前任务列表的最终汇总。"""
    all_tasks = task_store.list_tasks(session_id=session_id)
    if not all_tasks:
        return

    print(f"\n{color_text('助手：任务执行完成，结果如下：', INFO_COLOR)}")
    for task in all_tasks:
        print(f"- [{task['status']}] {task['description']}")


@dataclass
class CliCommand:
    """交互式命令定义。"""
    name: str
    description: str
    handler: Callable[["InteractiveSession", str], bool]
    aliases: List[str] = field(default_factory=list)


class InteractiveSession:
    """管理交互式命令注册与分发。"""

    def __init__(
        self,
        task_store: TaskStore,
        background_job_store: BackgroundJobStore,
        exec_agent: ExecuteAgent,
        plan_agent: PlanAgent,
    ) -> None:
        self.task_store = task_store
        self.background_job_store = background_job_store
        self.exec_agent = exec_agent
        self.plan_agent = plan_agent
        self._commands: Dict[str, CliCommand] = {}
        self._command_order: List[CliCommand] = []

    def register_command(self, command: CliCommand) -> None:
        """注册命令及其别名。"""
        for command_name in [command.name, *command.aliases]:
            self._commands[command_name] = command
        self._command_order.append(command)

    def handle_input(self, user_input: str) -> Optional[bool]:
        """处理 slash 命令；非命令输入返回 None。"""
        text = user_input.strip()
        if not text.startswith("/"):
            return None

        parts = text.split(maxsplit=1)
        command_name = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        command = self._commands.get(command_name)
        if command is None:
            print_console_block(
                "命令提示",
                [f"未知命令：{command_name}", "输入 /help 或 /h 查看可用命令"],
                PLAN_COLOR,
            )
            return True

        return command.handler(self, args)

    def get_help_rows(self) -> List[List[str]]:
        """返回帮助信息表格行。"""
        rows: List[List[str]] = []
        for command in self._command_order:
            names = [command.name, *command.aliases]
            rows.append([", ".join(names), command.description])
        return rows

    def print_help(self) -> None:
        """打印所有已注册命令。"""
        print_info_table(self.get_help_rows())

    def reset_state(self) -> None:
        """清空当前会话与任务状态。"""
        self.task_store.reset()
        self.exec_agent.reset_conversation()
        self.plan_agent.reset_conversation()


def summarize_background_job(job: Dict[str, Any]) -> str:
    """把后台作业摘要成一行文本。"""
    command = str(job.get("command", "")).strip()
    if len(command) > 72:
        command = command[:69] + "..."
    return (
        f"[{job.get('status')}] id={job.get('id')} pid={job.get('pid')} "
        f"({job.get('pid_role', 'launcher')}) cwd={job.get('cwd')} | {command}"
    )


def build_background_job_result_summary(jobs: List[Dict[str, Any]]) -> str:
    """生成适合写入任务结果的后台作业摘要。"""
    if not jobs:
        return ""

    lines = ["后台作业："]
    for job in jobs:
        command = str(job.get("command", "")).strip()
        if len(command) > 72:
            command = command[:69] + "..."
        lines.append(
            f"- job_id={job.get('id')} pid={job.get('pid')} "
            f"status={job.get('status')} stdout={job.get('stdout_log')} "
            f"stderr={job.get('stderr_log')} command={command}"
        )
    return "\n".join(lines)


def handle_help_command(session: InteractiveSession, _: str) -> bool:
    """显示所有可用命令。"""
    session.print_help()
    return True


def handle_new_command(session: InteractiveSession, _: str) -> bool:
    """开启一轮新的会话与任务上下文。"""
    session.reset_state()
    print_console_block("状态", ["已开启新的会话和任务上下文"], INFO_COLOR)
    return True


def handle_clear_logs_command(session: InteractiveSession, _: str) -> bool:
    """一键清除所有日志与缓存：agent 日志、task、history、background_jobs、background logs。"""
    try:
        # 清空 .logs/agent 下的按天日志，保留当天文件并截断，其他文件直接删除
        current_log_file = get_current_agent_log_path()
        agent_log_count = 0
        if current_log_file.exists():
            current_log_file.write_text("", encoding="utf-8")
            agent_log_count += 1
        for archived_log in _AGENT_LOG_DIR.glob("*.log"):
            if archived_log == current_log_file or not archived_log.is_file():
                continue
            try:
                archived_log.unlink()
                agent_log_count += 1
            except OSError:
                logger.warning("删除 agent 日志失败：%s", archived_log)

        # 清空任务与后台作业
        session.task_store.reset()
        log_count = session.background_job_store.clear_all()

        # 清空历史并重置 PlanAgent 会话
        session.plan_agent.history_store.clear_all()
        session.plan_agent.messages = list(session.plan_agent.base_messages)
        session.plan_agent.history_messages = list(session.plan_agent.base_messages)
        session.plan_agent.current_session_id = session.plan_agent.history_store.start_session(
            session.plan_agent.agent_name,
            session.plan_agent.history_messages,
        )
        session.plan_agent.latest_usage = None
        session.plan_agent.latest_turn_metrics = None

        session.exec_agent.reset_conversation()

        print_console_block(
            "清除完成",
            [
                f".logs/agent：已清理 {agent_log_count} 个日志文件",
                f"task.json：已清空",
                f"history.json：已清空",
                f"background_jobs.json：已清空",
                f".logs/background：已删除 {log_count} 个日志文件",
            ],
            INFO_COLOR,
        )
    except Exception as exc:
        logger.exception("清除日志失败")
        print_console_block("清除失败", [str(exc)], PLAN_COLOR)
    return True


def handle_exit_command(session: InteractiveSession, _: str) -> bool:
    """结束交互式会话。"""
    del session
    return False


def handle_export_command(session: InteractiveSession, args: str) -> bool:
    """导出当前对话，或在传入 --all 时导出全部对话。"""
    export_all = False
    raw_path = ""
    try:
        parts = shlex.split(args)
        for part in parts:
            if part == "--all":
                export_all = True
                continue
            if raw_path:
                raise ValueError("`/export` 只支持一个导出路径参数，可选附加 `--all`。")
            raw_path = part

        if raw_path:
            export_path = safe_resolve_path(raw_path)
            if export_path.suffix.lower() != ".md":
                export_path = export_path.with_suffix(".md")
        else:
            export_path = build_default_export_path()

        exported = session.plan_agent.export_history_markdown(
            export_path,
            export_all=export_all,
        )
    except Exception as exc:
        print_console_block("导出失败", [str(exc)], PLAN_COLOR)
        return True

    print_console_block(
        "导出完成",
        [
            f"已导出 PlanAgent 上下文到：{to_workspace_relative(exported)}",
            f"导出范围：{'所有对话' if export_all else '当前对话'}",
            f"历史源文件：{_HISTORY_FILE}",
        ],
        INFO_COLOR,
    )
    return True


def handle_jobs_command(session: InteractiveSession, args: str) -> bool:
    """显示后台作业摘要。"""
    raw_limit = args.strip()
    try:
        limit = int(raw_limit) if raw_limit else 10
        jobs = session.background_job_store.refresh_jobs(limit=limit)
    except Exception as exc:
        print_console_block("后台作业", [str(exc)], PLAN_COLOR)
        return True

    if not jobs:
        print_console_block("后台作业", ["当前没有后台作业记录"], INFO_COLOR)
        return True

    lines = [summarize_background_job(job) for job in jobs]
    print_console_block("后台作业", lines, INFO_COLOR)
    return True


def handle_job_log_command(session: InteractiveSession, args: str) -> bool:
    """显示后台作业最近日志。"""
    parts = args.split()
    if not parts:
        print_console_block(
            "命令提示",
            ["用法：/job-log <job_id> [stdout|stderr|both] [tail_lines]"],
            PLAN_COLOR,
        )
        return True

    job_id = parts[0]
    stream = "both"
    tail_lines = 80
    for part in parts[1:]:
        if part in {"stdout", "stderr", "both"}:
            stream = part
            continue
        try:
            tail_lines = int(part)
        except ValueError:
            print_console_block("命令提示", [f"无法识别参数：{part}"], PLAN_COLOR)
            return True

    job = session.background_job_store.refresh_job(job_id)
    if job is None:
        print_console_block("后台日志", [f"未找到后台作业：{job_id}"], PLAN_COLOR)
        return True

    lines = [f"作业状态：{job['status']}"]
    if stream in {"stdout", "both"}:
        lines.append(f"stdout: {job['stdout_log']}")
        stdout_text = read_log_tail(Path(job["stdout_log"]), tail_lines) or "<empty>"
        lines.extend(stdout_text.splitlines())
    if stream in {"stderr", "both"}:
        lines.append(f"stderr: {job['stderr_log']}")
        stderr_text = read_log_tail(Path(job["stderr_log"]), tail_lines) or "<empty>"
        lines.extend(stderr_text.splitlines())
    print_console_block("后台日志", lines, INFO_COLOR)
    return True


def handle_stop_job_command(session: InteractiveSession, args: str) -> bool:
    """停止指定后台作业。"""
    job_id = args.strip()
    if not job_id:
        print_console_block("命令提示", ["用法：/stop-job <job_id>"], PLAN_COLOR)
        return True

    job = session.background_job_store.refresh_job(job_id)
    if job is None:
        print_console_block("后台作业", [f"未找到后台作业：{job_id}"], PLAN_COLOR)
        return True
    if job["status"] != "running":
        print_console_block(
            "后台作业",
            [f"后台作业 {job_id} 当前状态为 {job['status']}，无需停止"],
            INFO_COLOR,
        )
        return True

    ok, details = stop_background_process(int(job["pid"]))
    if ok or not is_process_running(int(job["pid"])):
        updated = session.background_job_store.update_status(
            job_id,
            "stopped",
            stopped_at=time.time(),
        )
        lines = [
            f"已停止后台作业：{updated['id']}",
            f"PID：{updated['pid']}",
            f"状态：{updated['status']}",
        ]
        if details:
            lines.append(details)
        print_console_block("后台作业", lines, INFO_COLOR)
        return True

    print_console_block("后台作业", [details or "停止失败"], PLAN_COLOR)
    return True


def register_default_commands(session: InteractiveSession) -> None:
    """注册内置交互式命令。"""
    session.register_command(
        CliCommand(
            name="/help",
            aliases=("/h",),
            description="显示所有可用命令",
            handler=handle_help_command,
        )
    )
    session.register_command(
        CliCommand(
            name="/new",
            description="开启一轮新的会话和任务上下文",
            handler=handle_new_command,
        )
    )
    session.register_command(
        CliCommand(
            name="/clear-logs",
            description="一键清除所有日志与缓存（.logs/agent、task、history、jobs、.logs/background）",
            handler=handle_clear_logs_command,
        )
    )
    session.register_command(
        CliCommand(
            name="/export",
            description="导出当前对话为 Markdown；加 --all 导出所有对话",
            handler=handle_export_command,
        )
    )
    session.register_command(
        CliCommand(
            name="/jobs",
            description="显示后台作业列表，可选传入数量上限",
            handler=handle_jobs_command,
        )
    )
    session.register_command(
        CliCommand(
            name="/job-log",
            description="查看后台作业日志：/job-log <job_id> [stdout|stderr|both] [tail]",
            handler=handle_job_log_command,
        )
    )
    session.register_command(
        CliCommand(
            name="/stop-job",
            description="停止后台作业：/stop-job <job_id>",
            handler=handle_stop_job_command,
        )
    )
    session.register_command(
        CliCommand(
            name="/exit",
            description="退出当前交互会话",
            handler=handle_exit_command,
            aliases=["/quit"],
        )
    )


def main() -> None:
    """启动交互式命令行入口。"""
    task_store = TaskStore()
    background_job_store = BackgroundJobStore()
    exec_agent = ExecuteAgent(task_store, background_job_store=background_job_store)
    plan_agent = PlanAgent(task_store, background_job_store=background_job_store)
    plan_agent.register_tool(
        ExecuteNextTaskTool(
            task_store,
            exec_agent,
            session_id_provider=lambda: plan_agent.current_session_id,
        )
    )
    session = InteractiveSession(
        task_store,
        background_job_store,
        exec_agent,
        plan_agent,
    )
    register_default_commands(session)

    print_info_table(
        [
            ["欢迎语", "欢迎使用 Agent 交互终端"],
            ["当前系统", get_system_name()],
            ["当前工作区", str(WORKSPACE_DIR)],
            ["任务文件", str(_TASK_FILE)],
            ["后台作业", str(_BACKGROUND_JOBS_FILE)],
            ["命令帮助", "输入 /help 或 /h 查看可用命令"],
        ]
    )

    while True:
        user_input = input("\n用户：")
        command_result = session.handle_input(user_input)
        if command_result is not None:
            if not command_result:
                break
            continue

        # 1. 规划任务
        plan_agent.chat(
            user_input,
            reset_history=False,
        )
        if not task_store.has_active_tasks(session_id=plan_agent.current_session_id):
            exec_agent.reset_conversation()


def test_search_code_tool() -> Dict[str, Any]:
    """按当前项目内置风格对 SearchCodeTool 做自测。"""

    tool = SearchCodeTool()
    cases: List[Dict[str, Any]] = []

    def run_case(name: str, parameters: Dict[str, Any], checker: Callable[[Dict[str, Any]], None]) -> None:
        raw = tool.run(parameters)
        parsed = json.loads(raw)
        checker(parsed)
        cases.append(
            {
                "name": name,
                "passed": True,
                "parameters": parameters,
                "result": parsed,
            }
        )

    run_case(
        "精确匹配 main.py 中的类定义",
        {
            "query": "class SearchCodeTool(BaseTool):",
            "path": "main.py",
            "regex": False,
            "case_sensitive": True,
        },
        lambda parsed: (
            parsed.get("success") is True
            and parsed.get("data")
            and parsed["data"][0]["file"] == "main.py"
        )
        or (_ for _ in ()).throw(AssertionError("精确匹配未返回 main.py 的命中结果")),
    )

    run_case(
        "大小写不敏感匹配",
        {
            "query": "searchcodetool",
            "path": "main.py",
            "regex": False,
            "case_sensitive": False,
        },
        lambda parsed: (
            parsed.get("success") is True
            and any(
                "SearchCodeTool" in item.get("snippet", "")
                for item in parsed.get("data", [])
            )
        )
        or (_ for _ in ()).throw(AssertionError("大小写不敏感匹配未命中 SearchCodeTool")),
    )

    run_case(
        "正则匹配类定义",
        {
            "query": r"class\s+SearchCodeTool\b",
            "path": "main.py",
            "regex": True,
            "case_sensitive": True,
        },
        lambda parsed: (
            parsed.get("success") is True
            and any(
                re.search(r"class\s+SearchCodeTool\b", item.get("snippet", ""))
                for item in parsed.get("data", [])
            )
        )
        or (_ for _ in ()).throw(AssertionError("正则匹配未命中类定义")),
    )

    run_case(
        "非法正则应返回失败",
        {
            "query": "[unclosed",
            "path": "main.py",
            "regex": True,
            "case_sensitive": True,
        },
        lambda parsed: (
            parsed.get("success") is False
            and "invalid regex" in str(parsed.get("error", ""))
        )
        or (_ for _ in ()).throw(AssertionError("非法正则没有正确返回失败信息")),
    )

    with tempfile.TemporaryDirectory(dir=str(WORKSPACE_DIR)) as temp_dir:
        temp_root = Path(temp_dir)
        rel_temp_root = temp_root.resolve().relative_to(WORKSPACE_DIR.resolve()).as_posix()
        (temp_root / "a.py").write_text("needle_py = 1\n", encoding="utf-8")
        (temp_root / "b.txt").write_text("needle_py = 1\n", encoding="utf-8")

        run_case(
            "glob 过滤只返回 .py 文件",
            {
                "query": "needle_py",
                "path": rel_temp_root,
                "glob": "*.py",
                "regex": False,
                "case_sensitive": True,
            },
            lambda parsed: (
                parsed.get("success") is True
                and len(parsed.get("data", [])) == 1
                and parsed["data"][0]["file"].endswith("/a.py")
            )
            or (_ for _ in ()).throw(AssertionError("glob 过滤没有只返回 a.py")),
        )

    workspace_dir = WORKSPACE_DIR / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    ignored_file = workspace_dir / "search_code_tool_ignored_case.py"
    ignored_token = f"search_code_tool_should_be_ignored_{uuid.uuid4().hex}"
    try:
        ignored_file.write_text(f"{ignored_token} = True\n", encoding="utf-8")
        run_case(
            "忽略 .gitignore 忽略目录下的文件",
            {
                "query": ignored_token,
                "path": ".",
                "glob": "*.py",
                "regex": False,
                "case_sensitive": True,
            },
            lambda parsed: (
                parsed.get("success") is True and not parsed.get("data")
            )
            or (_ for _ in ()).throw(AssertionError("被忽略目录中的文件仍然被搜索到了")),
        )
    finally:
        ignored_file.unlink(missing_ok=True)
        print(f"临时文件已被删除: {ignored_file}")

    report = {
        "success": True,
        "case_count": len(cases),
        "cases": cases,
    }
    return report


def run_tests() -> int:
    """运行内置测试并在结束后清理临时结果文件。"""
    _TEMP_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _TEMP_DIR / f"test-report-{uuid.uuid4().hex}.json"

    try:
        report = test_search_code_tool()
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[TEST] test_search_code_tool passed ({report['case_count']} cases)")
        return 0
    except Exception as exc:
        failure_report = {
            "success": False,
            "test": "test_search_code_tool",
            "error": str(exc),
        }
        report_path.write_text(
            json.dumps(failure_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[TEST] test_search_code_tool failed: {exc}", file=sys.stderr)
        return 1
    finally:
        print(f"临时报告已被删除: {report_path}")
        report_path.unlink(missing_ok=True)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test",
        action="store_true",
        help="运行内置测试并退出",
    )
    return parser.parse_args(argv)


def cli() -> int:
    """命令行入口。"""
    args = parse_args()
    if args.test:
        return run_tests()
    main()
    return 0



if __name__ == "__main__":
    raise SystemExit(cli())