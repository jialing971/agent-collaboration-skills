# Agent Collaboration Skills

让 Codex 与 Claude Code 在同一个本地项目中交接任务、保存进度，并从上次审核位置继续工作。

| 技能 | 解决的问题 | 入口 |
|---|---|---|
| project-relay | Codex 规划与审核，Claude 执行；记录接管、检查点和审核基线 | [技能说明](skills/project-relay/SKILL.md) |
| shared-project-memory | 两端共享项目摘要、决策与验证记录，按项目和分支检索 | [技能说明](skills/shared-project-memory/SKILL.md) |

两个技能可以分别安装。一起使用时，接力工具管理任务状态，共享记忆保存长期摘要。它们不调用额外的记忆模型 API，也不需要 pip 包。

## 支持范围

- Python 3.10+，仅使用标准库。
- Windows PowerShell 5.1+。本版 project-relay 的所有命令均仅支持 Windows，包含本地状态命令；自动执行使用 Windows Job Object 管理进程树。
- Git：读取项目分支和审核提交时需要。共享记忆也支持普通文件夹。
- 实际派活还需要使用者自己的 Claude Code，以及当前实现支持的 CC Switch `claude-desktop` 代理配置。模型费用按使用者的服务计费。
- 共享记忆 Python CLI 可直接运行；本仓库的首次发布验证以 Windows 为范围，其他系统请自行验证。

## 安装

把仓库链接交给 Codex：

```text
请使用 $skill-installer 从 https://github.com/jialing971/agent-collaboration-skills
安装 skills/project-relay 和 skills/shared-project-memory，并检查本机运行依赖。
```

也可以下载 ZIP，或克隆后手动安装。下面的 PowerShell 命令会克隆仓库并复制到 Codex 的用户级技能目录；若同名技能已存在会停止，请先核对旧版本。

```powershell
git clone https://github.com/jialing971/agent-collaboration-skills.git
Set-Location agent-collaboration-skills
$skillDestination = Join-Path $env:USERPROFILE '.agents\skills'
New-Item -ItemType Directory -Path $skillDestination -Force | Out-Null
foreach ($skillName in @('project-relay', 'shared-project-memory')) {
    $skillTarget = Join-Path $skillDestination $skillName
    if (Test-Path -LiteralPath $skillTarget) { throw "技能已存在，请先核对：$skillTarget" }
    Copy-Item -LiteralPath (Join-Path '.\skills' $skillName) -Destination $skillTarget -Recurse
}
```

Claude Code 使用其用户级目录 `$env:USERPROFILE\.claude\skills`，按相同方式复制两个技能目录。需要两端始终使用同一份源码时，可自行建立指向同一实体目录的目录链接。修改技能后若未显示，请新开会话或重启宿主。

安装只是加载操作方法。自动保存项目摘要、执行模型任务和修改项目文件，仍遵循当前用户授权。仓库不会修改全局 `AGENTS.md`、`CLAUDE.md`、系统 PATH 或模型路由。

## 更新

已安装副本不会自动跟随仓库变化。更新时先确认工作区干净并记录当前提交，再执行 `git pull --ff-only`；随后把 `skills/project-relay` 与 `skills/shared-project-memory` 重新复制或重新指向更新后的目录，设置 `PROJECT_RELAY_PYTHON` / `SHARED_PROJECT_MEMORY_PYTHON` 等已有环境变量保持不变。重新打开宿主会话或重启宿主后生效。

更新后可对源目录与安装副本做哈希核对，并运行本仓库测试；实际使用前再按维护文档做一次最小真实任务验证。维护与发布流程见 [维护说明](skills/project-relay/references/maintenance.md)。

## 配置 Python

两个 PowerShell 入口优先使用下面的环境变量，值为已安装 Python 的可执行文件绝对路径。以下路径是示例，请替换为自己的 Python 3.10+：

```powershell
$env:PROJECT_RELAY_PYTHON = 'C:\Python312\python.exe'
$env:SHARED_PROJECT_MEMORY_PYTHON = 'C:\Python312\python.exe'
```

这只设置当前 PowerShell 进程的环境。由桌面应用启动的技能需在其运行环境中设置，或让助手用已确认的 Python 直接运行 `.py` 入口。

没有设置覆盖变量时，入口尝试当前用户的 `.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。找不到时清楚报错；显式设置了无效路径也会报错，不悄悄改用其他 Python。

## 共享记忆：先保存，再读取

以下命令从仓库根目录运行。使用技能安装副本时，将脚本路径替换为实际安装位置。项目目录、摘要文件必须已存在，`--store` 等全局参数放在子命令之前。

```powershell
$memoryScript = '.\skills\shared-project-memory\scripts\memory.ps1'
& $memoryScript --project 'D:\Projects\demo' context
& $memoryScript --project 'D:\Projects\demo' save --agent codex --title '阶段交接' --file 'D:\Projects\demo\阶段摘要.md'
& $memoryScript --project 'D:\Projects\demo' recall --limit 5
& $memoryScript --project 'D:\Projects\demo' search --query '相机 超时' --limit 5
```

Claude 保存时用 `--agent claude`。两端读取同一份数据库，默认位于当前用户的 `.shared-project-memory/data/memory.sqlite3`。项目与分支隔离，Git worktree 可共享项目身份；多个关键词需同时命中。`context` 和空库读取不会创建数据库。可用 `--store '数据库绝对路径'` 指定自己的库。

## 项目接力：任务与审核

以下本地状态命令无需调用模型：

```powershell
$relayScript = '.\skills\project-relay\scripts\relay.ps1'
& $relayScript -Project 'D:\Projects\demo' init
& $relayScript -Project 'D:\Projects\demo' new --title '实现功能' --plan-file 'D:\Projects\demo\实施计划.md'
# 将下列 ID 替换为 new 返回的 task.id
& $relayScript -Project 'D:\Projects\demo' takeover --task '任务ID' --agent claude
& $relayScript -Project 'D:\Projects\demo' checkpoint --task '任务ID' --agent claude --file 'D:\Projects\demo\阶段摘要.md'
& $relayScript -Project 'D:\Projects\demo' handoff --task '任务ID' --brief
```

计划写明目标、允许修改的范围和验收。任务状态与证据存入实际项目的 `协作记录/`。代码必须位于同一个实际项目目录，或另行通过 Git 同步；记录不会迁移运行中的进程、未落盘对话或终端状态。

模型间交接和 `run` 默认使用 `handoff --brief`：只包含任务身份与完整计划、最近一个检查点、最近一条 Codex 审核/返工意见、最近一次执行的 ok/error/结束时间与证据路径、审核起点、当前分支和简短改动概况，不携带历史 events、stdout/stderr、tool payload 或 live JSONL，也不调用模型做摘要。计划超过 12000 字符会明确拒绝并提示先整理计划；整体序列化上下文超过 20000 字符同样在启动执行器、创建执行批次或改变任务状态之前拒绝，不会静默截断验收条件。返回中的 `context_chars` / `context_limit` 是实际字符数与上限，不是精确 token 数。需要人工追溯全部事件、多份检查点和原始输出时，使用不带 `--brief` 的完整 `handoff`。

自动派活前，复制 [config.example.json](skills/project-relay/config.example.json) 到当前用户的 `.project-relay/config.json`，填写自己的绝对路径和预期路由。也可通过 `--config` 使用其他位置。JSON 中的占位路径不会展开环境变量。

| 配置字段 | 内容 |
|---|---|
| `command` | Claude Code 可执行文件及可选前置参数的数组 |
| `desktop_config` | 当前生效的 Claude 桌面第三方配置 JSON 的绝对路径 |
| `cc_switch_state` | CC Switch 当前路由状态 JSON 的绝对路径 |
| `cc_switch_db` | 可选的 CC Switch 数据库绝对路径；填写时从该库读取当前网关凭据 |
| `desktop_meta` | 可选的桌面配置元数据 JSON；存在时校验 `appliedId` |
| `expected_provider_id` | 使用者已核对的 CC Switch 提供商 ID |
| `model` | 桌面配置中的模型别名 |
| `expected_upstream_model` | 对应该模型别名的预期上游模型 |
| `dashboard` | 可选布尔值，默认未配置即 `false`；设为 `true` 时 `run` 会启动或复用只读看板并打开当前任务 |

当前实现会校验 CC Switch `apps.claude-desktop` 路由、回环 HTTP 代理、已应用配置及模型映射。未配置 `cc_switch_db` 时沿用桌面配置中的网关凭据；配置后每次启动都以只读方式从该 SQLite 库读取 `settings.claude_desktop_gateway_token`，令牌轮换后无需重启即可生效，且不会创建或修改数据库。数据库不可读、令牌缺失或为空白时明确拒绝，不回退到桌面旧凭据。凭据仅注入执行器子进程，不写入配置示例或执行证据。其他 CC Switch 版本或网关格式可能需要适配，`doctor` 不通过时先核对配置，勿直接绕过校验。

`doctor` 只校验配置、文件路径、路由和模型映射，不发起模型请求，因此不能证明当前认证可用；真实认证只在 `run` 时暴露。若上游返回 401，执行器内部可能重试，外层表现为本次运行超时，需结合证据判断，不能仅凭超时认定网络问题。

```powershell
& $relayScript -Project 'D:\Projects\demo' --config 'D:\Config\relay.json' doctor
# 获得当前用户对实际模型调用和费用的授权后再执行：
& $relayScript -Project 'D:\Projects\demo' --config 'D:\Config\relay.json' run --task '任务ID' --timeout 900 --max-turns 20
```

`run` 成功且状态为 `pending_review` 只表示等待复核。Codex 应独立检查文件、运行适当测试；在工作区干净且提交固定后，使用 `approve --task ... --agent codex --revision ... --report ...` 记录审核基线。角色参数是操作约定，不能认证调用者身份。执行器拥有本地文件与命令权限；进程树管理不提供文件访问隔离。

`run` 还使用流式输出，把每次派活写成独立 attempt 的 `协作记录/<task>/live-<attempt>.jsonl`。执行器运行期间即可看到工具调用、工具结果、公开 assistant 文本/文本增量、stderr 和最终 result；thinking 内容不写入。合法的 `system/thinking_tokens` 统计不进入 live 或证据操作流，也不会被当作协议错误；只有 `system/init` 表示会话初始化，`status` 和 `task_summary` 分别按状态和任务摘要归类。运行结束仍保留 `执行-<attempt>.json` 证据，成功判定继续要求返回码 0、`is_error: false`、`subtype: success` 且没有权限拒绝。运行时网关 token 在写入 live、证据和 HTTP 状态前替换。

阶段复核（不固定提交基线）使用：

```powershell
& $relayScript -Project 'D:\Projects\demo' review --task '任务ID' --agent codex --outcome passed --file 'D:\Projects\demo\阶段审核.md'
& $relayScript -Project 'D:\Projects\demo' review --task '任务ID' --agent codex --outcome changes_requested --file 'D:\Projects\demo\返工.md'
```

`review` 要求 Codex 是当前负责人且没有存活的执行进程。`passed` 把状态置为 `reviewed`（阶段复核通过，待最终验收），`changes_requested` 置为 `changes_requested`；两者都不写 `reviewed_sha` 或审核基线，只有 `approve` 才会置为 `approved`。第 2 次执行或返工状态会在看板中显示为返工中。

## 只读看板

看板显示派活目标与验收、任务阶段、Claude 实时操作与日志、Codex 复核与返工记录。它只读取当前项目已有的 SQLite 和 live JSONL，不提供网页派活、暂停、终止或审批入口。阶段使用中文标签；`review passed` 显示为“阶段复核通过，待最终验收”，只有 `approve` 后才是“最终验收通过”。工具与事件摘要由本地确定性规则生成，不调用模型做总结。

```powershell
# 启动或复用本项目的本机只读服务，并返回 url / pid / port / token
& $relayScript -Project 'D:\Projects\demo' dashboard --task '任务ID' --no-open
# 不指定任务时打开任务列表：dashboard --no-open
# 显式打开浏览器：去掉 --no-open
# 停止本项目看板服务
& $relayScript -Project 'D:\Projects\demo' dashboard --stop
```

服务默认绑定 `127.0.0.1`；`--port 0` 自动分配端口。URL 带随机能力 token，API 只接受本机 `Host`/`Origin`，没有写接口和宽松 CORS，静态资源为固定白名单。服务进程独立于本次派活的 Windows Job Object，关闭浏览器不会停止 Claude；需要停止时使用 `dashboard --stop`。服务日志集中在 `协作记录/看板/`，不继承执行器注入的模型 token 环境，也不读取模型配置。读取既有 live 文件时会先忽略 `system/thinking_tokens` 统计噪声，再决定最近 5000 条窗口，不改写历史证据原件；页面把相邻文本增量合并为一条持续输出，不展示 thinking，也不为每个增量单独占行。单次执行超过 5000 条有效事件时，接口返回最近 5000 条及窗口偏移，页面按偏移删除头部旧行后继续追加新事件，保留滚动位置和展开状态。

在用户配置中把 `dashboard` 设为 `true` 后，每次 `run` 都会启动或复用看板、通过系统默认浏览器打开对应任务，并在返回 JSON 中提供 `dashboard_url`；未配置或为 `false` 时不启动服务。看板启动失败会在 `run` 结果中给出 `dashboard_error`，不会返回不可用的 URL；显式 `dashboard` 命令启动失败则直接报错。任务状态和实时数据约 1 秒刷新，最后事件时间、断线、控制进程退出、失败和不完整尾行都会显示。

## 验证与贡献

从仓库根目录，使用已确认的 Python 3.10+ 运行：

```powershell
& $env:PROJECT_RELAY_PYTHON -X utf8 -B -m unittest discover -s tests -v
```

本套测试应在 Windows 上运行。测试在临时目录中执行，覆盖中文路径、记忆保存/检索/隔离、接管、审核边界、模拟执行器超时和进程清理。模拟程序不调用真实模型；测试通过不代表使用者的真实 Claude/CC Switch 路由已验证。发布边界见 [release-scope.md](docs/release-scope.md)，术语见 [GLOSSARY.md](GLOSSARY.md)。

仓库的 GitHub Actions 工作流在 `windows-latest` 上以 Python 3.10 和 3.12 两个版本运行同一测试命令，两个版本互不取消；工作流只读仓库内容，不安装 pip 依赖，不读取使用者配置，也不调用模型：

```powershell
python -X utf8 -B -m unittest discover -s tests -v
```

维护、同步和发布检查清单见 [project-relay 维护说明](skills/project-relay/references/maintenance.md)。

提交问题时提供操作系统、Python 版本、脱敏命令和错误。项目摘要、数据库、真实执行日志、配置凭据与缓存不属于技能源码，请留在自己的受控目录。

## 许可证

[MIT](LICENSE) © 2026 jialing971。
