# Background Runtime 优化计划

## 1. 背景与目标

这份文档用于规划 `main.py` 中“长命令 / 后台任务 / 服务启动 / 日志读取 / 停止任务 / 就绪判断”相关能力的下一阶段重构与收敛。

写这份计划的直接原因有三点：

1. 当前项目已经具备后台服务能力，但这层能力以 `start_background_service`、`read_background_job_log`、`stop_background_job` 等专用工具直接暴露给模型，模型在规划与执行时容易过度显式地讨论“后台作业”。
2. 实际体验中，这套能力与 Cursor / Claude Code / Codex CLI 的体感不同。它们并不是没有后台能力，而是更多把后台命令管理下沉到终端 runtime、任务系统或产品交互层，而不是暴露成很多专门工具。
3. 当前实现能工作，但还存在设计债务，比如：
   - 背景服务与普通长命令在语义上区分过重。
   - PlanAgent 容易把“启动服务”拆成重复任务。
   - 日志查询和完成通知的责任边界还不够清晰。
   - 测试覆盖不足，README 的描述与代码现状也有出入。

本计划的核心目标不是“删除后台能力”，而是把它从“模型直接操作的一组专用服务工具”，逐步收敛为“runtime 级的通用后台任务能力”。

一句话概括目标状态：

**保留后台任务能力，弱化专用 service 工具感，增强 runtime 自动管理、通知和可观测性，让模型更多关心‘做什么’，更少关心‘怎么盯进程’。**

## 2. 当前现状总结

当前项目背景能力大致如下：

- 通过 `run_command` 运行同步命令。
- 通过 `start_background_service` 启动常驻服务，并内置 readiness 等待逻辑。
- 通过 `list_background_jobs` / `read_background_job_log` / `stop_background_job` 查询和管理后台作业。
- `BackgroundJobStore` 将作业信息持久化到 `.agent/background_jobs.json`。
- 日志写入 `.logs/background/`。
- PlanAgent / ExecuteAgent 通过 prompt 规则知道何时用后台服务工具。

当前已经确认的问题：

### 2.1 规划层问题

- PlanAgent 容易把“启动服务”拆成多个重复任务。
- 用户明明已经给出“启动项目服务”这类模糊但低风险请求，模型仍可能先进行通用追问。
- 模型会显式暴露内部“后台作业”概念，而不是更自然地给出“服务已启动、稍后会通知/可查看日志”。

### 2.2 执行层问题

- “启动服务”和“启动普通长命令”目前是两个不同心智模型。
- 如果只是想让一个长命令后台跑完并在结束后通知，现在缺少一个更通用、统一的路径。
- 对于“服务启动后就不必立即返回完整日志”的场景，模型仍可能走专门日志查询工具，导致对话显得啰嗦。

### 2.3 runtime 层问题

- 当前 `background_job` 更偏“服务管理对象”，而不是“通用后台任务对象”。
- background 生命周期、通知、输出读取、结果摘要之间耦合还比较散。
- 目前我们已经增强了 `process_group_id` 支持，但这是局部修补，不是完整重构。

### 2.4 文档与测试问题

- 当前仓库没有真正覆盖 `background_service` 相关工具的内置测试。
- `README.md` 已经宣称覆盖“后台任务启动与记录 / 日志读取 / 等待服务就绪”等验证，但代码中尚无对应测试实现。

## 3. 参考对标：Claude Code 的做法

通过阅读 `claude-code` 源码，可以得到几条关键结论：

### 3.1 Claude Code 不是没有后台能力

它的 `BashTool` 直接支持 `run_in_background` 参数，并在 prompt 中明确告诉模型：

- 长命令如果不需要立即拿结果，用 `run_in_background`
- 不要用 sleep 轮询
- 完成后会收到通知
- 持续流式观察用 Monitor，而不是反复查日志

### 3.2 Claude Code 的后台能力是 runtime 内建能力

从 `LocalShellTask` 可以看出，它不是单纯 shell `&`：

- 会给后台任务分配 task id
- 会有输出文件
- 会记录状态
- 会支持 kill
- 会在完成后自动通知
- 会在疑似交互式卡住时给出 stall notification

### 3.3 Claude Code 的设计重点

它更像下面这套心智模型：

- 普通命令：前台执行
- 长命令：`BashTool(run_in_background=true)`
- 持续观察：Monitor
- 完成通知：runtime 主动发
- 用户交互：`Ctrl+B` 可把前台任务转后台

### 3.4 对本项目最有价值的启发

不是照搬所有功能，而是吸收以下思想：

1. 后台能力应该尽量归到“通用命令执行 runtime”，而不是堆很多专用工具名。
2. 后台任务完成最好由 runtime 主动通知，而不是模型主动轮询。
3. “服务已就绪”和“命令已完成”是两种不同事件，需要在 runtime 里区分。
4. 持续流式监控和一次性后台执行最好拆成两种能力，而不是混在同一个工具里。

## 4. 建议的目标架构

建议将未来的后台相关能力收敛到三层：

### 4.1 模型层

模型层只需要理解少量概念：

- 同步命令执行
- 后台运行命令
- 查询后台任务
- 停止后台任务
- 可选：监控后台任务输出

模型不应该再频繁显式思考：

- “这是 background_job 还是 task”
- “我现在是否要读 stderr_log 路径”
- “我要不要再调用一次 read_background_job_log 拿同样的信息”

### 4.2 工具层

未来工具层建议收敛成更接近下面的形式：

- `run_command`
  - 支持同步执行
  - 支持后台执行，例如 `background=true`
  - 支持可选等待模式，例如：
    - `wait_mode="none"`：立刻返回 task/job id
    - `wait_mode="exit"`：等命令完成
    - `wait_mode="ready"`：用于服务启动时等 ready
- `list_background_jobs`
- `read_background_job_log`
- `stop_background_job`
- 可选新增：`monitor_background_job`

也就是说，未来可以把 `start_background_service` 的大部分语义并入更通用的 `run_command`，而不是永远保留一个“服务专用工具”。

### 4.3 runtime 层

runtime 层负责：

- 后台任务注册与持久化
- 输出落盘
- 状态刷新
- 进程组管理
- 完成通知
- 可选的 stall / interactive prompt 检测
- 服务就绪判断

这层应尽量少暴露实现细节给模型。

## 5. 推荐演进方向

不建议一次性推倒重做，建议采用“兼容旧工具 + 分阶段下沉”的方案。

### 阶段 A：稳固现有实现

目标：在不改大接口的前提下，把现在这套能力补稳。

工作项：

1. 补齐后台任务测试。
2. 修正文档与测试事实不一致的问题。
3. 完善 `BackgroundJobStore` 的状态刷新与进程组逻辑。
4. 统一 background job 的结果摘要格式，减少模型误读。
5. 为失败启动、ready 超时、端口未开、日志提示 ready 等情况补充更明确的结构化字段。

验收标准：

- 当前 `start_background_service` 相关路径有最小测试覆盖。
- 失败/超时/ready/停止状态都能稳定复现并通过测试。
- README 中的能力描述与实际代码一致。

### 阶段 B：收敛工具语义

目标：让“后台运行命令”成为 `run_command` 的原生能力，而不是服务专用能力。

建议方案：

1. 给 `run_command` 增加后台参数，例如：
   - `background: bool = False`
   - `wait_mode: "exit" | "none" | "ready"`
   - `ready_check` 结构，例如：
     - `port`
     - `host`
     - `startup_timeout`
     - `poll_interval`
2. 把 `start_background_service` 内部逻辑复用到 `run_command(background=True, wait_mode="ready")`。
3. 保留 `start_background_service` 一段时间，但内部改为兼容包装器，逐步降级为历史接口。

这样做的好处：

- 模型只需要一个“命令执行工具”，认知负担更小。
- 服务启动只是命令执行的一种等待模式，而不是完全独立的工具心智。
- 更接近 Claude Code 的 `BashTool + run_in_background` 思路。

### 阶段 C：增加通知优先、轮询最少的机制

目标：让模型更少主动查日志，更多依赖 runtime 结果与通知。

建议能力：

1. 后台任务完成后写入结构化通知事件。
2. 如果是服务启动类任务，在 ready 时也可产生一条结构化通知。
3. `ExecuteAgent` 在收到“后台已启动、ready 已确认”的结构化结果后，直接写任务结果，不再额外补一轮“再看日志确认”。
4. 如需持续观察，再引入单独 monitor 概念，不要让普通日志工具承担 streaming 语义。

### 阶段 D：优化 prompt 与任务规划

目标：减少模型过度拆解与重复读取。

建议修改方向：

1. PlanAgent：
   - “启动服务”默认规划成一个完整任务。
   - 先检查常见入口，再决定是否追问。
   - 不为 ready 确认额外拆单独任务。
2. ExecuteAgent：
   - 后台任务一旦返回 `ready=True`，默认视为该任务已完成主要目标。
   - 没有新增信号时，不要再重复读取相同日志。
   - 普通后台命令若已 `background=true`，默认等待 runtime 通知，不主动 sleep 轮询。

### 阶段 E：可选引入 Monitor 能力

只有在你后续明确需要以下场景时，才建议新增：

- 持续跟踪某个后台输出流
- 只要出现某些日志模式就通知
- 监控 long-running watcher / 日志 tail / webhook poll

如果没有这些需求，可以先不做 Monitor。

因为对于当前项目而言，最重要的是把“一次性后台执行 + 服务 ready”做稳，而不是先扩展更多能力面。

## 6. 具体设计建议

### 6.1 后台任务对象的统一结构

建议长期保留如下字段：

- `id`
- `command`
- `pid`
- `pid_role`
- `process_group_id`
- `cwd`
- `status`
- `created_at`
- `updated_at`
- `stopped_at`
- `stdout_log`
- `stderr_log`
- `mode`
  - 可选值：`command` / `service` / `monitor`
- `ready`
- `ready_at`
- `ready_source`
  - 可选值：`port` / `log` / `manual` / `unknown`
- `exit_code`
- `summary`

这样未来不必依赖日志文本推断一切。

### 6.2 建议的 `run_command` 扩展参数

未来可考虑如下输入结构：

```json
{
  "command": "npm run dev",
  "background": true,
  "wait_mode": "ready",
  "ready_check": {
    "host": "localhost",
    "port": 5173,
    "startup_timeout": 30,
    "poll_interval": 1
  }
}
```

或对于普通后台任务：

```json
{
  "command": "npm run build",
  "background": true,
  "wait_mode": "none"
}
```

### 6.3 `start_background_service` 的处理建议

短期建议：

- 继续保留，避免立即破坏现有 prompt 和模型习惯。

中期建议：

- 将其内部实现改成 `run_command(..., background=true, wait_mode="ready")` 的包装器。

长期建议：

- 如果模型与文档都已迁移完成，可考虑将其降级为兼容接口，甚至最终移除。

### 6.4 `read_background_job_log` 的定位

建议明确定位为：

- 只读工具
- 主要用于用户显式查询
- 或执行失败时定位问题

不建议再让模型把它当作后台任务完成后的默认二次确认手段。

### 6.5 `stop_background_job` 的定位

这项能力需要保留。

原因：

- 用户需要显式停止 dev server / watcher。
- Runtime 自己也可能在清理阶段使用。
- 与 Cursor / Claude Code 的用户体验对齐时，后台任务“可停”是基本能力。

但后续应进一步增强：

- 区分“停止进程组”和“停止单进程”
- 返回更明确的停止来源与实际效果
- 对“已经退出”“找不到”“权限不足”给出结构化字段

## 7. 测试计划

当前这部分是明显短板，建议优先补。

### 7.1 必测用例

#### A. 启动后台命令

- 能创建 job 记录
- 能落盘 stdout/stderr 日志
- 能返回 `job_id`
- 在类 Unix 系统上能记录 `process_group_id`

#### B. 服务 ready 检测

- 端口 ready 时返回 `ready=True`
- 仅日志 ready 时返回 `ready=True`
- 超时时返回 `timed_out=True`
- 进程提前退出时返回 `verification="process_exited"`

#### C. 日志读取

- 能读 stdout
- 能读 stderr
- `tail_lines` 生效
- job 不存在时返回失败

#### D. 停止后台任务

- 运行中任务可以停止
- 已退出任务不会报致命错误
- 进程组停止优先于单 pid 停止
- 旧记录没有 `process_group_id` 时仍能回退兼容

#### E. Prompt / 规划行为

如果后续给 PlanAgent 加行为测试，至少要覆盖：

- “启动项目服务”不再拆成重复的 ready 确认任务
- 已能从 `package.json` 等入口确定启动命令时，不再多轮追问

### 7.2 测试组织建议

当前项目是单文件架构，测试也可先延续内置自测风格，不必急着拆出复杂目录。

建议短期在 `main.py` 里新增类似：

- `test_background_job_runtime()`
- `test_wait_for_background_service()`
- `test_read_background_job_log_tool()`
- `test_stop_background_job_tool()`

中期如果测试越来越多，再考虑单独迁移到 `tests/`。

## 8. prompt 与产品体验优化清单

### 8.1 PlanAgent

需强化的规则：

- 启动服务默认一个任务完成，不拆重复子任务。
- 先检查项目入口，再追问。
- 如果工具已经能等待 ready，就不要再单独规划“再等一下看看”。

### 8.2 ExecuteAgent

需强化的规则：

- 如果后台工具已返回 `ready=True`，默认不再额外读同一份日志确认。
- 如果后台命令以 `background=true` 启动，且用户未要求跟踪过程，不要主动轮询。
- 日志读取仅用于失败定位或用户主动查询。

### 8.3 用户体验

建议最终体感接近下面这种表达：

- “服务已启动，地址是 `http://localhost:5173/`。”
- “任务已转入后台，完成后我会通知你。”
- “后台任务已结束，退出码 0。”

而不是：

- “我现在创建一个后台作业”
- “让我再次读取后台日志确认一次”
- “我再执行一个等待服务启动完成的子任务”

## 9. 迁移策略

建议采用兼容迁移，不要一次性替换所有接口。

### 迁移顺序建议

1. 先补测试和文档。
2. 再扩展 `run_command` 的后台能力。
3. 再把 `start_background_service` 改成包装器。
4. 再更新 prompt，使模型优先走统一路径。
5. 最后再决定是否保留专用服务工具接口。

### 兼容要求

- 旧的 `.agent/background_jobs.json` 记录必须能读。
- 旧记录没有 `process_group_id` 时应平滑兼容。
- 旧 prompt 仍提到 `start_background_service` 时，工具不能立刻消失。

## 10. 风险与注意事项

### 10.1 不要过早引入太多新抽象

这个项目的约束是“单文件、轻量、可读、可改”，所以不建议为了追求“更像大型产品”而引入大量额外模块。

应优先：

- 重用现有数据结构
- 小步演进
- 兼容旧行为

### 10.2 不要把 Monitor 先做复杂

Monitor 是锦上添花，不是当前第一优先级。

当前更值得做的是：

- 统一后台任务模型
- 减少模型轮询
- 强化 ready / exit / failed 通知

### 10.3 不要让 prompt 过度依赖实现细节

prompt 应描述能力边界和优先策略，而不应让模型记太多内部字段、日志路径、存储细节。

## 11. 推荐的下一轮执行清单

如果后续开新窗口开始优化，建议按下面顺序推进：

### 第一轮

1. 为 background runtime 补最小测试。
2. 修正 README 中关于已有测试覆盖的描述。
3. 统一后台 job 结果摘要与失败字段。

### 第二轮

1. 给 `run_command` 增加后台模式与等待模式。
2. 复用 `start_background_service` 逻辑到统一运行入口。
3. 保持旧接口兼容。

### 第三轮

1. 调整 PlanAgent / ExecuteAgent prompt。
2. 减少“启动服务”重复规划。
3. 降低模型主动日志轮询频率。

### 第四轮

1. 评估是否需要 `monitor`。
2. 评估是否保留 `start_background_service` 为公开工具。
3. 如果迁移稳定，再决定是否简化工具面。

## 12. 最终建议

对于本项目，不建议删除后台能力。

更合适的路线是：

**保留后台任务 runtime，减少专用 service 工具感，把“后台执行”逐步收敛为通用命令工具的能力，把通知、输出、状态、停止更多地下沉到 runtime。**

这条路线既能保留当前项目真实可用的长命令处理能力，也更接近 Claude Code 这种产品的总体设计风格。
