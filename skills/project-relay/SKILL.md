---
name: project-relay
description: 在本机项目中由 Codex 规划和审核、Claude Code 经 CC Switch 的 DeepSeek 执行；支持 Codex 额度不足时由 Claude 独立接管，以及额度恢复后的回审。用于用户要求开发接力、派活或接管已有任务时。
---

# 开发接力

运行环境：本技能的所有命令仅支持 Windows，包含本地状态与交接命令。

唯一代码源在本 Skill 目录。两端共享同一实际项目目录的 `协作记录/state.sqlite3`；`handoff` 返回计划、完整检查点和审核起点。切换工具不需要重放内部对话。

## 入口

根据本技能文件的位置定位 `scripts/relay.ps1`，使用 `-Project '实际项目根目录' <命令>`。PowerShell 入口优先使用环境变量 `PROJECT_RELAY_PYTHON` 指定的 Python 可执行文件；未配置时使用当前用户的 Codex bundled Python。缺失时请用户配置 Python 3.10+ 的绝对路径，不修改系统 PATH。也可用已确认的 Python 直接运行 `scripts/relay.py --project '项目目录' <命令>`。

命令相对本 Skill 目录解析；不要把文档里的“项目目录”作为实际路径。

## 开始或接回

1. 运行 `status`。若尚未初始化，运行 `init`。结合已有 `shared-project-memory` 读取项目历史，核对实际分支和文件。
2. 对已有任务运行 `handoff --task <ID>`，读取各检查点、执行证据和 `review_from`。任务绑定创建时的分支；当前分支与任务分支不一致时 `handoff` 会拒绝，先切回原分支。运行 `takeover --task <ID> --agent codex` 或 `--agent claude` 明确接管。仍有执行进程时等待其结束，不绕过占用。
3. 用户有新目标时，在 `协作记录/<问题名>/` 写明目标、允许修改范围、验收与测试命令。用 `new --title '问题名' --plan-file '计划绝对路径'` 建任务。`new` 初始负责人为 Codex；Claude 独立建任务后立即接管。

## Codex 在线时派活

Codex 完成需求澄清和计划后，先 `doctor` 核对 CC Switch 路由，再 `run --task <ID> --timeout 900 --max-turns 20`。这是启动真实 Claude Code，按用户现有 DeepSeek 接入计费。执行器使用本机文件和命令工具，工作目录是指定项目；它不是操作系统安全沙箱。

默认配置位于当前用户目录的 `.project-relay/config.json`，也可在子命令之前传 `--config '配置绝对路径'`。配置只存执行器路径、预期路由和凭据来源文件的位置。使用者按本技能目录中的 `config.example.json` 创建自己的配置。凭据在启动时读取现有桌面配置，仅注入子进程。路由或模型映射变化时先重新核对，不擅自切换提供商或修改 Codex 模型配置。

`run` 返回 `ok: true` 且任务状态为 `pending_review`，表示本次执行器正常结束、结果等待 Codex 复核；`interrupted` 表示失败、超时或中断，需要读证据核查文件。结果正文在返回的 `evidence` 文件中。执行器成功不等于需求验收通过。路由校验失败时不会启动执行器，拒绝原因写入任务目录的 `执行拒绝-*.json` 并记录 `execution_rejected` 事件，任务负责人和状态保持不变。

`run` 的执行进程树随运行结束或控制进程退出而关闭；需要持续运行的服务应另外启动并交接，不作为本次派活的后台遗留进程。

Codex 接回后独立读改动和运行适当测试。需要返工时写明确检查点再派活；同一问题最多自动返工两轮，仍失败则向用户说明阻塞。执行器不能凭自己的声明完成最终审核。

## Codex 额度不足

Claude 可在桌面 Local Code 选择同一项目，读取本 Skill 并接管；直接自行澄清、规划、实现和测试，不等待 Codex、不调用 `run` 自我委派。新决策和修改保持待 Codex 复核。

每完成一个实际阶段，将“目标与约束、已完成、实际验证、未验证和下一步”写入任务问题目录的检查点文件，运行 `checkpoint --task <ID> --agent claude --file '检查点绝对路径'`。Codex 保存自己的阶段时改用 `--agent codex`。完成阶段时也按现有共享记忆技能追加摘要；记录库不代替代码同步。

`checkpoint` 保存的阶段结果进入 `pending_review`，只表示“等待 Codex 复核的阶段结果”；执行仍在进行时保持 `running`。checkpoint 不能证明执行调用成功，真实执行是否成功以 `run` 返回的 `ok` 和 `evidence` 为准。

保存应在每个阶段完成时发生，不依赖额度耗尽通知。突然退出只能恢复到最近成功保存的文件和检查点。`takeover` 会识别已退出的控制/执行进程并记录中断；仍有子进程时拒绝接管。

## 最终审核与基线

只有实际 Codex 完成独立审核后，接管为 codex，保存审核意见，再对干净的代码提交运行：

`approve --task <ID> --agent codex --revision <完整HEAD> --report '审核意见绝对路径'`

该命令记录审核证据和基线，不证明调用者身份；两端须按真实角色操作。批准前要求任务起点和 `review_from` 都属于当前 HEAD 历史；历史重写或回退到任务起点之前时明确拒绝，须人工核对。未提交代码可以给出审核意见，但提交基线保持待固定；无 Git 目录仍可交接，不能宣称有提交基线。下次审核从 `review_from` 以及未提交、未跟踪代码开始核对。不会自动 push、merge 或部署。

## 运行位置变化

桌面升级可能使 bundled CLI 路径失效。`doctor` 失败时重新定位版本并核对当前 CC Switch 路由，再只更新 relay 的非敏感配置。两端使用同一份实际技能源；移动前更新目录链接和说明。新会话显式使用 `$project-relay` / `/project-relay` 最可靠，自动选择由宿主决定。自动派活的进程保护仅支持 Windows；运行前需取得当前用户对模型调用及其费用的授权。
