# project-relay 公开仓库维护

来源仓库：[jialing971/agent-collaboration-skills](https://github.com/jialing971/agent-collaboration-skills)。公开源码作为维护基线，本机技能安装副本由它同步。维护时保持公开版通用入口和 Windows 平台约束，不把个人环境写进跟踪文件。

## 1. 确认基线

1. 确认当前分支、工作区状态和远端基线：`git fetch` 后核对 `git rev-parse HEAD` 与 `origin/main`，记录本次审核起点 SHA。
2. 只从允许修改的文件开始；公开版与个人安装版存在差异时逐段迁移，不整文件覆盖，保留公开版的通用入口、平台保护和说明。
3. 提交前检查跟踪文件，确认没有真实凭据、个人绝对路径、执行日志、数据库或缓存。配置示例只放通用占位。

## 2. 先回归，再实现

1. 在新的行为修复前，先把对应用例加入测试并运行，确认失败；保存脱敏后的红灯输出。
2. 实现后先运行相关用例转绿，再运行全量测试。固定命令：

```powershell
python -X utf8 -B -m unittest discover -s tests -v
```

3. 测试只使用临时目录、临时数据库和模拟执行器，不读取真实凭据、不调用模型。Windows 专属用例由平台保护跳过，其他系统不能据此宣称通过。
4. 本地中间证据写入被 Git 忽略的记录目录，不进入公开提交。

## 3. 提交与推送

1. 只提交用户已授权的源码、测试和通用文档；逐项核对 `git status` 与 `git diff`。
2. 提交信息说明行为变化和验证方式；记录提交 SHA，作为后续 CI 跟踪和复核的固定点。
3. 推送前确认用户当前授权范围。推送后用提交 SHA 跟踪远端状态，不用“本地通过”替代远端结果。

## 4. 跟踪 CI 直到结束

1. 用推送的完整 SHA 查询工作流运行，并等待到结束：

```powershell
gh run list --commit <完整SHA>
gh run watch <run-id> --exit-status
```

2. 工作流在 `windows-latest` 上运行 Python 3.10 与 3.12 两个矩阵任务，`fail-fast: false`，任一失败都要读取对应任务的完整日志，不只看汇总状态。
3. 失败时定位根因，修复后重新运行回归和全量测试，再推送并继续按新 SHA 跟踪，直到两个矩阵任务都通过。
4. CI 只运行标准库测试，不安装 pip 依赖、不读取使用者配置、不调用模型。CI 通过只证明公开测试通过，不代表真实认证或真实路由已验证。

## 5. 同步实际技能安装副本

1. 以仓库中的 `skills/project-relay` 为准，同步本机 Codex 与 Claude 的技能安装副本；使用目录链接时保持链接指向同一实体目录，复制时排除 `__pycache__` 和记录文件。
2. 保留 `PROJECT_RELAY_PYTHON` 等 Python 入口环境变量支持，不因更新改写用户环境或系统 PATH。
3. 对源目录与安装副本做哈希核对，确认脚本、配置示例和技能说明一致：

```powershell
Get-ChildItem -Recurse -File skills/project-relay |
    Where-Object { $_.FullName -notmatch '__pycache__' } |
    Get-FileHash -Algorithm SHA256
```

4. 同步后由 Claude 侧执行一次真实最小任务，确认执行通道可用：`doctor` 只校验配置，不能代替认证验证；实际 `run` 需在用户已授权模型调用和费用后进行，并检查返回状态与证据文件。若上游 401 触发内部重试，外层可能表现为超时，应读证据判断。
5. 更新完成后重新打开宿主会话，确认技能说明与脚本均从新副本加载。

## 6. 发布边界

公开仓库只发布技能代码、通用文档、配置示例和测试。个人数据库、真实凭据、项目协作记录、执行日志、缓存和作者专用路径不发布。更新说明、CI 矩阵和维护入口同步写入根 `README.md`，发布范围写入 `docs/release-scope.md`；后续维护遵循用户当前授权，不再只按首次发布处理。
