# 第三方代码来源与适配

本项目复用 [simple10/agents-observe](https://github.com/simple10/agents-observe) 的摘要与事件状态派生逻辑。

- 上游快照：`b3a7238b3913976c10da052f75c73e29ad084c61`
- 许可：MIT，Copyright (c) 2025 Joe Johnston；完整文本见同目录 `agents-observe-LICENSE.txt`。
- 复用来源文件：
  - `app/client/src/agents/claude-code/helpers.ts`
  - `app/client/src/agents/claude-code/process-event.ts`

## 适配范围

`skills/project-relay/dashboard/app.js` 将以下 TypeScript 工具改写为浏览器原生 JavaScript：

- `oneLine`：折叠空白并去除简单 Markdown 标记。
- `relativePath`：按项目根目录显示相对路径，并适配 Windows 反斜杠。
- `extractBashBinary`：从 Bash 命令提取二进制名。
- `getToolSummary`：为 Bash、Read、Write、Edit、Grep、Glob、Agent、Skill、WebSearch、WebFetch、NotebookEdit 生成单行摘要。
- `eventStatus`：由事件类型和 payload 派生 running / completed / failed。

只保留只读看板所需的分支；去除了上游的 React、筛选器、图标注册表、StructuredOutput 专用分支和宿主 hook 接入。中文标签、配色、布局与 relay 的事件结构由本项目实现。没有引入上游的 Docker、前端构建链或任何 npm 运行依赖。
