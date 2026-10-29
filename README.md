# Agent Collaboration Skills

让 Codex 与 Claude Code 在同一个本地项目中交接任务、保存进度，并从上次审核位置继续工作。

| 技能 | 解决的问题 | 入口 |
|---|---|---|
| project-relay | Codex 规划与审核，Claude 执行；记录接管、检查点和审核基线 | [技能说明](skills/project-relay/SKILL.md) |
| shared-project-memory | 两端共享项目摘要、决策与验证记录，按项目和分支检索 | [技能说明](skills/shared-project-memory/SKILL.md) |

两个技能可以分别安装。一起使用时，接力工具管理任务状态，共享记忆保存长期摘要。它们不调用额外的记忆模型 API，也不需要 pip 包。

## 支持范围

- Python 3.10+，仅使用标准库。
- Windows PowerShell 5.1+。project-relay 的自动执行使用 Windows Job Object 管理进程树，本版只支持 Windows 自动派活。
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
& $relayScript -Project 'D:\Projects\demo' handoff --task '任务ID'
```

计划写明目标、允许修改的范围和验收。任务状态与证据存入实际项目的 `协作记录/`。代码必须位于同一个实际项目目录，或另行通过 Git 同步；记录不会迁移运行中的进程、未落盘对话或终端状态。

自动派活前，复制 [config.example.json](skills/project-relay/config.example.json) 到当前用户的 `.project-relay/config.json`，填写自己的绝对路径和预期路由。也可通过 `--config` 使用其他位置。JSON 中的占位路径不会展开环境变量。

| 配置字段 | 内容 |
|---|---|
| `command` | Claude Code 可执行文件及可选前置参数的数组 |
| `desktop_config` | 当前生效的 Claude 桌面第三方配置 JSON 的绝对路径 |
| `cc_switch_state` | CC Switch 当前路由状态 JSON 的绝对路径 |
| `desktop_meta` | 可选的桌面配置元数据 JSON；存在时校验 `appliedId` |
| `expected_provider_id` | 使用者已核对的 CC Switch 提供商 ID |
| `model` | 桌面配置中的模型别名 |
| `expected_upstream_model` | 对应该模型别名的预期上游模型 |

当前实现会校验 CC Switch `apps.claude-desktop` 路由、回环 HTTP 代理、已应用配置及模型映射。它读取桌面配置中的网关凭据，并仅注入执行器子进程；配置例子不包含凭据。其他 CC Switch 版本或网关格式可能需要适配，`doctor` 不通过时先核对配置，勿直接绕过校验。

```powershell
& $relayScript -Project 'D:\Projects\demo' --config 'D:\Config\relay.json' doctor
# 获得当前用户对实际模型调用和费用的授权后再执行：
& $relayScript -Project 'D:\Projects\demo' --config 'D:\Config\relay.json' run --task '任务ID' --timeout 900 --max-turns 20
```

`run` 成功且状态为 `pending_review` 只表示等待复核。Codex 应独立检查文件、运行适当测试；在工作区干净且提交固定后，使用 `approve --task ... --agent codex --revision ... --report ...` 记录审核基线。角色参数是操作约定，不能认证调用者身份。执行器拥有本地文件与命令权限；进程树管理不提供文件访问隔离。

## 验证与贡献

从仓库根目录，使用已确认的 Python 3.10+ 运行：

```powershell
& $env:PROJECT_RELAY_PYTHON -X utf8 -B -m unittest discover -s tests -v
```

测试在临时目录中运行，覆盖中文路径、记忆保存/检索/隔离、接管、审核边界、模拟执行器超时和进程清理。模拟程序不调用真实模型；测试通过不代表使用者的真实 Claude/CC Switch 路由已验证。发布边界见 [release-scope.md](docs/release-scope.md)，术语见 [GLOSSARY.md](GLOSSARY.md)。

提交问题时提供操作系统、Python 版本、脱敏命令和错误。项目摘要、数据库、真实执行日志、配置凭据与缓存不属于技能源码，请留在自己的受控目录。

## 许可证

[MIT](LICENSE) © 2026 jialing971。
