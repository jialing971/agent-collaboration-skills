/*
 * 接力看板只读前端。
 * 摘要与事件状态派生逻辑适配自 simple10/agents-observe
 * (MIT, 快照 b3a7238b3913976c10da052f75c73e29ad084c61)：
 * oneLine / relativePath / extractBashBinary / getToolSummary / eventStatus。
 * 完整许可与适配范围见 skills/project-relay/third_party/。
 */
(() => {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const token = params.get("token") || "";
  let selectedTask = params.get("task") || "";
  let data = null;
  let polling = false;
  let timer = null;
  let autoSelectDone = false;
  let taskFilter = "";
  let logFilter = "";
  let selectedAttemptId = null;
  let planExpanded = true;
  let autoFollow = true;
  let stick = true;
  let renderedAttempt = null;
  let renderedOffset = 0;
  let renderedCount = 0;
  let lastAttemptSignature = "";
  let lastReviewSignature = "";
  const openReviewKeys = new Set();

  const projectName = document.getElementById("project-name");
  const taskSearch = document.getElementById("task-search");
  const taskList = document.getElementById("task-list");
  const taskTitle = document.getElementById("task-title");
  const taskSubtitle = document.getElementById("task-subtitle");
  const connection = document.getElementById("connection");
  const connectionText = document.getElementById("connection-text");
  const statusBanner = document.getElementById("status-banner");
  const stagebar = document.getElementById("stagebar");
  const planToggle = document.getElementById("plan-toggle");
  const planBody = document.getElementById("plan-body");
  const planText = document.getElementById("plan-text");
  const planMeta = document.getElementById("plan-meta");
  const reviewList = document.getElementById("review-list");
  const attemptSelect = document.getElementById("attempt-select");
  const logFilterInput = document.getElementById("log-filter");
  const autoFollowInput = document.getElementById("auto-follow");
  const logStatus = document.getElementById("log-status");
  const logList = document.getElementById("log-list");
  const logEmpty = document.getElementById("log-empty");

  const STEPS = ["派活", "执行", "阶段复核", "最终验收"];

  const KIND_LABELS = {
    started: "开始",
    system: "系统",
    tool_use: "工具",
    tool_result: "结果",
    assistant: "回复",
    assistant_delta: "输出",
    stderr: "错误输出",
    result: "最终结果",
    finished: "结束",
    parse_error: "解析失败",
    error: "错误",
    event: "事件",
  };

  const REVIEW_LABELS = {
    review_passed: "阶段复核通过",
    review_changes_requested: "要求返工",
    approved: "最终验收通过",
    checkpoint: "阶段检查点",
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function shortId(value) {
    const text = String(value || "");
    return text.length > 8 ? text.slice(0, 8) : text;
  }

  function oneLine(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/\*\*(.*?)\*\*/g, "$1")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/^[-*] /gm, "")
      .replace(/\s*\n\s*/g, " ")
      .trim();
  }

  function shorten(value, max) {
    const text = String(value === undefined || value === null ? "" : value);
    return text.length > max ? text.slice(0, max) + "…" : text;
  }

  function relativePath(file, cwd) {
    if (!file) return "";
    const path = String(file).replace(/\\/g, "/");
    if (!cwd) return path;
    const root = String(cwd).replace(/\\/g, "/").replace(/\/+$/, "");
    if (!root) return path;
    if (path.toLowerCase() === root.toLowerCase()) return ".";
    if (path.toLowerCase().startsWith(root.toLowerCase() + "/")) {
      return path.slice(root.length + 1);
    }
    return path;
  }

  const VALID_BINARY_RE = /^[a-zA-Z0-9][a-zA-Z0-9._-]*$/;

  function extractBashBinary(command) {
    const first = String(command || "").split("\n")[0].trim();
    const tokens = first.split(/\s+/);
    let skipNext = false;
    for (const token of tokens) {
      if (skipNext) {
        skipNext = false;
        continue;
      }
      if (token.includes("=") || token === "&&" || token === ";" || token === "||") continue;
      if (token.startsWith("$(") || token.startsWith("`")) continue;
      if (token === "cd") {
        skipNext = true;
        continue;
      }
      if (["for", "do", "done", "if", "then", "else", "fi", "while", "case", "esac"].includes(token)) {
        continue;
      }
      const binary = token.replace(/^.*[\\/]/, "");
      if (binary && VALID_BINARY_RE.test(binary)) return binary;
    }
    return null;
  }

  function getToolSummary(toolName, input, cwd) {
    const toolInput = input && typeof input === "object" ? input : {};
    switch (toolName) {
      case "Bash": {
        const description = toolInput.description;
        const command = toolInput.command;
        const binary = command ? extractBashBinary(command) : null;
        const prefix = binary ? `[${binary}] ` : "";
        if (description) return prefix + description;
        return command ? prefix + command.replace(/\s*\n\s*/g, " \\n ").trim() : "";
      }
      case "Read":
      case "Write":
        return relativePath(toolInput.file_path, cwd);
      case "Edit":
        return relativePath(toolInput.file_path, cwd);
      case "Grep": {
        const pattern = toolInput.pattern;
        const path = toolInput.path ? relativePath(toolInput.path, cwd) : "";
        if (pattern && path) return `/${pattern}/ in ${path}`;
        if (pattern) return `/${pattern}/`;
        return "";
      }
      case "Glob":
        return toolInput.pattern || "";
      case "Agent":
        return toolInput.description || toolInput.prompt || "";
      case "Skill":
        return toolInput.skill || "";
      case "WebSearch":
      case "WebFetch":
        return toolInput.query || toolInput.url || "";
      case "NotebookEdit":
        return relativePath(toolInput.notebook_path, cwd);
      default:
        return toolInput.description || toolInput.command || toolInput.query || "";
    }
  }

  function toolResultText(content) {
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      return content.map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item.text === "string") return item.text;
        return "";
      }).filter(Boolean).join("\n");
    }
    if (content && typeof content === "object") return JSON.stringify(content);
    return "";
  }

  function eventSummary(event) {
    const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
    switch (event.kind) {
      case "started":
        return `第 ${payload.attempt_number || "?"} 次执行开始 · ${payload.model || "模型未记录"}`;
      case "system": {
        const subtype = payload.subtype || "";
        const model = payload.model ? ` · ${payload.model}` : "";
        const session = payload.session_id ? ` · 会话 ${shortId(payload.session_id)}` : "";
        if (subtype === "init") return `会话初始化${model}${session}`;
        if (subtype === "status") {
          const state = payload.status ? ` · ${oneLine(payload.status)}` : "";
          return `状态${state}${session}`;
        }
        if (subtype === "task_summary") {
          const summary = payload.summary ? ` · ${oneLine(payload.summary)}` : "";
          return `任务摘要${summary}${session}`;
        }
        return `系统${subtype ? ` · ${subtype}` : ""}${session}`;
      }
      case "tool_use": {
        const summary = getToolSummary(payload.name, payload.input, data && data.project_root);
        return summary || payload.name || "工具调用";
      }
      case "tool_result": {
        const text = oneLine(toolResultText(payload.content));
        const prefix = payload.is_error ? "工具失败" : "工具结果";
        return text ? `${prefix}：${text}` : prefix;
      }
      case "assistant":
      case "assistant_delta":
        return oneLine(payload.text) || "…";
      case "stderr":
        return oneLine(payload.text) || "错误输出";
      case "result": {
        const text = oneLine(payload.result || payload.error);
        const subtype = payload.subtype ? ` · ${payload.subtype}` : "";
        return text ? `最终结果${subtype}：${text}` : `最终结果${subtype}`;
      }
      case "finished":
        return payload.ok
          ? "执行结束"
          : `执行结束（失败）：${oneLine(payload.error || "未知原因")}`;
      case "parse_error":
        return `无法解析的输出：${oneLine(payload.text)}`;
      case "error":
        return oneLine(payload.text || payload.error) || "错误";
      case "event":
        return [payload.type, payload.subtype].filter(Boolean).join(" · ") || "事件";
      default:
        return oneLine(payload.text || payload.message) || event.kind || "事件";
    }
  }

  function eventStatus(kind, payload) {
    const value = payload && typeof payload === "object" ? payload : {};
    if (kind === "tool_use") return "running";
    if (kind === "tool_result") return value.is_error ? "failed" : "completed";
    if (kind === "error" || kind === "parse_error" || kind === "stderr") return "failed";
    if (kind === "finished") return value.ok === false ? "failed" : "completed";
    if (kind === "result") return value.is_error === true ? "failed" : "completed";
    return "completed";
  }

  function detailContent(event) {
    const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
    if (event.kind === "assistant" || event.kind === "assistant_delta") {
      const text = String(payload.text || "");
      return text.length > 320 ? { label: "展开完整文本", text } : null;
    }
    if (event.kind === "stderr") {
      const text = String(payload.text || "");
      return text.length > 200 ? { label: "展开错误输出", text } : null;
    }
    if (Object.keys(payload).length === 0) return null;
    return { label: "查看详情", text: JSON.stringify(payload, null, 2) };
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    const today = new Date();
    if (date.toDateString() === today.toDateString()) {
      return date.toLocaleTimeString("zh-CN", { hour12: false });
    }
    return date.toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      second: "2-digit", hour12: false,
    });
  }

  function formatDateTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    });
  }

  function setConnection(online, lastEvent, message) {
    connection.classList.toggle("online", online);
    connection.classList.toggle("offline", !online);
    if (!online) {
      connectionText.textContent = message ? `连接断开：${message}` : "连接断开，正在重试";
      return;
    }
    const last = lastEvent ? `最后事件 ${formatTime(lastEvent)}` : "尚无事件";
    connectionText.textContent = `已连接 · ${last}`;
  }

  async function api(path) {
    const response = await fetch(path, {
      headers: { "X-Dashboard-Token": token },
      cache: "no-store",
    });
    const text = await response.text();
    let payload;
    try {
      payload = JSON.parse(text);
    } catch (error) {
      throw new Error("看板返回了无法解析的响应");
    }
    if (!response.ok) {
      throw new Error(payload.error || `请求失败（${response.status}）`);
    }
    return payload;
  }

  function updateUrl() {
    const next = new URLSearchParams();
    if (selectedTask) next.set("task", selectedTask);
    if (token) next.set("token", token);
    const query = next.toString();
    window.history.replaceState(null, "", query ? `?${query}` : window.location.pathname);
  }

  function selectTask(taskId) {
    selectedTask = taskId;
    selectedAttemptId = null;
    renderedAttempt = null;
    renderedOffset = 0;
    renderedCount = 0;
    lastAttemptSignature = "";
    lastReviewSignature = "";
    stick = true;
    logList.replaceChildren();
    updateUrl();
    pollNow();
  }

  function resetLogRender(attemptId) {
    renderedAttempt = attemptId || null;
    renderedOffset = 0;
    renderedCount = 0;
    logList.replaceChildren();
  }

  function renderTasks(tasks) {
    taskList.replaceChildren();
    const needle = taskFilter.toLowerCase();
    const visible = tasks.filter((task) => {
      if (!needle) return true;
      return `${task.title} ${task.id}`.toLowerCase().includes(needle);
    });
    if (visible.length === 0) {
      taskList.append(el("div", "muted small", tasks.length ? "没有匹配的任务" : "暂无任务"));
      return;
    }
    for (const task of visible) {
      const button = el("button", "task-item");
      button.type = "button";
      button.setAttribute("aria-current", task.id === selectedTask ? "true" : "false");
      button.append(el("span", "task-name", task.title));
      const meta = el("span", "task-meta");
      meta.append(el("span", "", task.id), el("span", "", task.owner || ""));
      button.append(meta);
      button.addEventListener("click", () => selectTask(task.id));
      taskList.append(button);
    }
  }

  function stagePosition(stage) {
    if (stage === "approved") return 3;
    if (stage === "reviewed") return 3;
    if (stage === "pending_review") return 2;
    if (stage === "running") return 1;
    if (stage === "changes_requested") return 1;
    if (stage === "interrupted") return 1;
    return 0;
  }

  function renderStage(task) {
    stagebar.replaceChildren();
    const stage = task.stage || "planned";
    const position = stagePosition(stage);
    const isError = stage === "interrupted" || stage === "changes_requested"
      || (stage === "running" && !task.execution_alive);
    let doneCount = position;
    let active = true;
    if (stage === "approved") {
      doneCount = STEPS.length;
      active = false;
    } else if (stage === "reviewed") {
      doneCount = 3;
      active = true;
    }
    STEPS.forEach((label, step) => {
      const node = el("span", "stage");
      node.append(el("span", "", label));
      if (stage === "approved" || step < doneCount) {
        node.classList.add("done");
      } else if (active && step === position) {
        node.classList.add(isError ? "error" : "active");
      }
      stagebar.append(node);
    });
    stagebar.append(el("span", "stage stage-status", task.status_label || task.status));
    if (task.rework) {
      stagebar.append(el("span", "stage stage-warn",
        `第 ${task.attempt_count} 次执行 · 返工中`));
    }
  }

  function applyPlanState() {
    planBody.classList.toggle("collapsed", !planExpanded);
    planToggle.textContent = planExpanded ? "收起" : "展开";
  }

  function detailsWithState(key, label, text) {
    const details = el("details");
    details.open = openReviewKeys.has(key);
    details.addEventListener("toggle", () => {
      if (details.open) openReviewKeys.add(key);
      else openReviewKeys.delete(key);
    });
    details.append(el("summary", "", label), el("pre", "", text));
    return details;
  }

  function reviewItem(key, kind, at, agent, body) {
    const item = el("div", "review-item");
    if (kind === "review_changes_requested") item.classList.add("requested");
    if (kind === "approved" || kind === "review_passed") item.classList.add("approved");
    const head = el("div", "review-head");
    head.append(el("span", "review-kind", REVIEW_LABELS[kind] || kind),
      el("span", "", formatDateTime(at)), el("span", "", agent || ""));
    item.append(head);
    const text = String(body || "");
    if (text.length > 180) {
      item.append(el("p", "review-body", shorten(text, 150)));
      item.append(detailsWithState(key, "展开完整记录", text));
    } else {
      item.append(el("p", "review-body", text || "（无正文）"));
    }
    return item;
  }

  function reviewSignature(taskData, reviews, checkpoints) {
    return JSON.stringify({
      rework: Boolean(taskData.rework),
      attempts: taskData.attempt_count || 0,
      reviews: reviews.map((item) => [item.sequence, item.kind, item.at, item.body]),
      checkpoints: checkpoints.map((item) => [item.sequence, item.at, item.body]),
    });
  }

  function renderReviews(taskData) {
    const reviews = Array.isArray(taskData.reviews) ? taskData.reviews : [];
    const checkpoints = Array.isArray(taskData.checkpoints) ? taskData.checkpoints : [];
    const signature = reviewSignature(taskData, reviews, checkpoints);
    if (signature === lastReviewSignature) return;
    lastReviewSignature = signature;
    const previousScroll = reviewList.scrollTop;
    reviewList.replaceChildren();
    const note = taskData.rework
      ? `返工流程：已执行 ${taskData.attempt_count} 次，等待 Codex 复核。`
      : "阶段复核记录意见；最终验收由 Codex 独立完成。";
    reviewList.append(el("p", "review-note", note));
    if (reviews.length === 0) {
      reviewList.append(el("p", "muted small", "暂无复核记录。阶段复核通过或最终验收后会显示在这里。"));
    } else {
      reviews.slice().reverse().forEach((item, index) => {
        const key = `review:${item.sequence || `${item.kind}:${item.at}:${index}`}`;
        reviewList.append(reviewItem(key, item.kind, item.at, item.agent, item.body));
      });
    }
    if (checkpoints.length > 0) {
      const block = el("div", "checkpoint-block");
      block.append(el("p", "review-note", "最近检查点"));
      checkpoints.slice(-3).reverse().forEach((item, index) => {
        const key = `checkpoint:${item.sequence || `${item.at}:${index}`}`;
        block.append(reviewItem(key, "checkpoint", item.at, item.agent, item.body));
      });
      reviewList.append(block);
    }
    reviewList.scrollTop = previousScroll;
  }

  function logAttemptSignature(attempts) {
    return attempts.map((item) => `${item.id}:${item.ended}`).join("|");
  }

  function renderAttemptOptions(attempts) {
    const signature = logAttemptSignature(attempts);
    if (signature !== lastAttemptSignature) {
      lastAttemptSignature = signature;
      attemptSelect.replaceChildren();
      for (const attempt of attempts) {
        const option = el("option");
        option.value = attempt.id;
        const state = attempt.ended ? (attempt.end_ok ? "完成" : "失败") : "进行中";
        option.textContent = `第 ${attempt.number} 次 · ${shortId(attempt.id)} · ${state}`;
        attemptSelect.append(option);
      }
    }
    attemptSelect.hidden = attempts.length <= 1;
    if (selectedAttemptId) attemptSelect.value = selectedAttemptId;
  }

  function setDeltaText(row, text) {
    row._deltaText = text;
    const summary = row.querySelector(".log-summary");
    if (summary) summary.textContent = shorten(oneLine(text) || "…", 500);
    const body = row.querySelector(".log-body");
    let details = row.querySelector("details");
    if (text.length > 320) {
      if (!details && body) {
        details = el("details");
        details.append(el("summary", "", "展开完整文本"), el("pre", "", text));
        body.append(details);
      } else if (details) {
        const pre = details.querySelector("pre");
        if (pre) pre.textContent = text;
      }
    } else if (details) {
      details.remove();
    }
    row.dataset.search = `${oneLine(text)} 输出 ${text}`.toLowerCase().slice(0, 5000);
  }

  function newDeltaRow(event, position) {
    const row = el("li", "log-row");
    row.dataset.kind = "assistant_delta";
    row.dataset.status = "completed";
    row.dataset.startAbs = String(position);
    row.dataset.endAbs = String(position);
    row.append(el("time", "", formatTime(event.at)));
    row.append(el("span", "kind", KIND_LABELS.assistant_delta));
    const body = el("div", "log-body");
    body.append(el("div", "log-summary", "…"));
    row.append(body);
    setDeltaText(row, String((event.payload && event.payload.text) || ""));
    return row;
  }

  function deltaRowFromEvents(events, from, to, startAbs) {
    let text = "";
    for (let index = from; index <= to; index += 1) {
      const payload = events[index] && events[index].payload;
      text += String((payload && payload.text) || "");
    }
    const row = newDeltaRow(events[from], startAbs);
    setDeltaText(row, text);
    row.dataset.endAbs = String(startAbs + (to - from));
    return row;
  }

  function appendLogEvent(event, position) {
    const last = logList.lastElementChild;
    if (event.kind === "assistant_delta" && last && last.dataset.kind === "assistant_delta"
        && Number(last.dataset.endAbs) === position - 1) {
      last.dataset.endAbs = String(position);
      setDeltaText(last, (last._deltaText || "") + String((event.payload && event.payload.text) || ""));
      return;
    }
    if (event.kind === "assistant_delta") {
      logList.append(newDeltaRow(event, position));
      return;
    }
    logList.append(renderLogRow(event, position));
  }

  function renderLogRow(event, position) {
    const row = el("li", "log-row");
    row.dataset.kind = event.kind || "event";
    row.dataset.status = eventStatus(event.kind, event.payload);
    row.dataset.startAbs = String(position);
    row.dataset.endAbs = String(position);
    row.append(el("time", "", formatTime(event.at)));
    row.append(el("span", "kind", KIND_LABELS[event.kind] || event.kind || "事件"));
    const body = el("div", "log-body");
    const summaryText = eventSummary(event);
    body.append(el("div", "log-summary", shorten(summaryText, 500)));
    const detail = detailContent(event);
    if (detail) {
      const details = el("details");
      details.append(el("summary", "", detail.label), el("pre", "", detail.text));
      body.append(details);
    }
    row.append(body);
    const payload = event.payload ? JSON.stringify(event.payload) : "";
    row.dataset.search = `${summaryText} ${KIND_LABELS[event.kind] || event.kind || ""} ${payload}`
      .toLowerCase().slice(0, 5000);
    return row;
  }

  function applyLogFilter() {
    const needle = logFilter.trim().toLowerCase();
    for (const row of logList.children) {
      row.hidden = Boolean(needle) && !(row.dataset.search || "").includes(needle);
    }
  }

  function renderLogs(taskData) {
    const attempts = Array.isArray(taskData.attempts) ? taskData.attempts : [];
    renderAttemptOptions(attempts);
    if (!selectedAttemptId || !attempts.some((item) => item.id === selectedAttemptId)) {
      selectedAttemptId = attempts.length ? attempts[attempts.length - 1].id : null;
      attemptSelect.value = selectedAttemptId || "";
    }
    const attempt = attempts.find((item) => item.id === selectedAttemptId);
    logEmpty.hidden = Boolean(attempt && attempt.events.length);
    if (!attempt) {
      logStatus.textContent = "该任务暂无实时日志。";
      resetLogRender();
      return;
    }
    const offset = Number.isInteger(attempt.event_offset) ? attempt.event_offset : 0;
    if (renderedAttempt !== attempt.id || attempt.events.length < renderedCount
        || offset < renderedOffset || offset > renderedOffset + renderedCount) {
      resetLogRender(attempt.id);
      renderedOffset = offset;
    } else if (offset > renderedOffset) {
      const previousTop = logList.scrollTop;
      const previousHeight = logList.scrollHeight;
      while (logList.firstElementChild
             && Number(logList.firstElementChild.dataset.endAbs) < offset) {
        logList.firstElementChild.remove();
      }
      const first = logList.firstElementChild;
      if (first && Number(first.dataset.startAbs) < offset) {
        const end = Number(first.dataset.endAbs);
        const next = first.nextSibling;
        const wasOpen = Boolean(first.querySelector("details")?.open);
        first.remove();
        const replacement = deltaRowFromEvents(attempt.events, 0, end - offset, offset);
        const replacementDetails = replacement.querySelector("details");
        if (replacementDetails) replacementDetails.open = wasOpen;
        logList.insertBefore(replacement, next);
      }
      renderedOffset = offset;
      const last = logList.lastElementChild;
      const covered = last ? Number(last.dataset.endAbs) + 1 : offset;
      renderedCount = Math.max(0, Math.min(covered - offset, attempt.events.length));
      if (!(autoFollow && stick)) {
        logList.scrollTop = Math.max(0, previousTop + (logList.scrollHeight - previousHeight));
      }
    }
    let appended = 0;
    for (let index = renderedCount; index < attempt.events.length; index += 1) {
      appendLogEvent(attempt.events[index], renderedOffset + index);
      appended += 1;
    }
    renderedCount = attempt.events.length;
    applyLogFilter();
    if (appended > 0 && autoFollow && stick) {
      logList.scrollTop = logList.scrollHeight;
    }
    const parts = [`第 ${attempt.number} 次执行 · ${shortId(attempt.id)}`];
    const sessionEvent = attempt.events.find((item) => item.session_id);
    if (sessionEvent) parts.push(`会话 ${shortId(sessionEvent.session_id)}`);
    if (attempt.incomplete_tail) parts.push("最后一行尚未写完整，已忽略");
    if (attempt.parse_errors) parts.push(`${attempt.parse_errors} 行无法解析`);
    if (attempt.truncated) parts.push("仅显示最近事件");
    if (attempt.ended) {
      parts.push(attempt.end_ok ? "本次执行已结束" : `本次执行失败：${attempt.end_error || "未知原因"}`);
    } else if (!taskData.execution_alive || attempt.id !== attempts[attempts.length - 1].id) {
      parts.push("本批次已中断，以下为已保留事件");
    } else {
      parts.push("执行进行中，约 1 秒刷新");
    }
    logStatus.textContent = parts.join(" · ");
  }

  function renderTask(taskData) {
    const task = taskData.task;
    taskTitle.textContent = task.title;
    taskSubtitle.textContent = [
      taskData.status_label || taskData.stage || task.status,
      `负责人 ${task.owner || "未记录"}`,
      task.branch ? `分支 ${task.branch}` : "无分支",
      `计划 ${(taskData.plan || "").length} 字`,
    ].join(" · ");

    let banner = "";
    if (task.status === "running" && !taskData.execution_alive) {
      banner = taskData.status_detail;
    } else if (taskData.stage === "interrupted") {
      banner = taskData.status_detail;
    } else if (task.status === "changes_requested") {
      banner = taskData.status_detail;
    } else if (taskData.stage === "reviewed") {
      banner = "阶段复核通过；等待 Codex 最终验收。";
    }
    statusBanner.textContent = banner;
    statusBanner.hidden = !banner;

    renderStage(taskData);
    const planValue = taskData.plan || "（任务没有计划正文）";
    if (planText.textContent !== planValue) planText.textContent = planValue;
    planMeta.textContent = `负责人 ${task.owner || "未记录"} · 完整计划正文 · `
      + (taskData.last_event_at ? `最后事件 ${formatTime(taskData.last_event_at)}` : "尚无事件");
    planToggle.hidden = false;
    applyPlanState();
    renderReviews(taskData);
    renderLogs(taskData);
  }

  function renderEmpty() {
    taskTitle.textContent = "接力看板";
    taskSubtitle.textContent = "选择左侧任务查看执行";
    statusBanner.hidden = true;
    stagebar.replaceChildren();
    lastReviewSignature = "";
    planText.textContent = "选择任务查看派活目标与验收。";
    planMeta.textContent = "";
    planToggle.hidden = true;
    reviewList.replaceChildren(el("p", "muted small", "选择任务查看复核记录。"));
    attemptSelect.hidden = true;
    logList.replaceChildren();
    logEmpty.hidden = false;
    logEmpty.textContent = "选择任务查看实时日志。";
    logStatus.textContent = "";
    resetLogRender();
  }

  function render(payload) {
    data = payload;
    projectName.textContent = payload.project || "只读";
    renderTasks(Array.isArray(payload.tasks) ? payload.tasks : []);
    if (!selectedTask && !autoSelectDone && payload.tasks && payload.tasks.length) {
      autoSelectDone = true;
      selectedTask = payload.tasks[payload.tasks.length - 1].id;
      updateUrl();
      window.setTimeout(pollNow, 0);
      return;
    }
    if (payload.task) {
      renderTask(payload);
    } else {
      renderEmpty();
    }
  }

  async function pollNow() {
    if (!token) {
      setConnection(false, null, "缺少访问 token");
      logEmpty.hidden = false;
      logEmpty.textContent = "请从 relay dashboard 返回的链接打开看板。";
      logStatus.textContent = "URL 中缺少 token，无法读取状态。";
      return;
    }
    if (polling) return;
    polling = true;
    try {
      const query = selectedTask ? `?task=${encodeURIComponent(selectedTask)}` : "";
      const payload = await api(`/api/state${query}`);
      render(payload);
      setConnection(true, payload.last_event_at);
    } catch (error) {
      setConnection(false, null, error.message);
    } finally {
      polling = false;
    }
  }

  function schedule() {
    window.clearTimeout(timer);
    timer = window.setTimeout(async () => {
      await pollNow();
      schedule();
    }, 1000);
  }

  taskSearch.addEventListener("input", () => {
    taskFilter = taskSearch.value.trim();
    if (data && Array.isArray(data.tasks)) renderTasks(data.tasks);
  });

  logFilterInput.addEventListener("input", () => {
    logFilter = logFilterInput.value.trim().toLowerCase();
    applyLogFilter();
  });

  autoFollowInput.addEventListener("change", () => {
    autoFollow = autoFollowInput.checked;
    if (autoFollow && stick) logList.scrollTop = logList.scrollHeight;
  });

  logList.addEventListener("scroll", () => {
    stick = logList.scrollHeight - logList.scrollTop - logList.clientHeight < 24;
  });

  attemptSelect.addEventListener("change", () => {
    selectedAttemptId = attemptSelect.value;
    resetLogRender();
    stick = true;
    if (data && data.task) renderLogs(data);
    if (autoFollow) logList.scrollTop = logList.scrollHeight;
  });

  planToggle.addEventListener("click", () => {
    planExpanded = !planExpanded;
    applyPlanState();
  });

  applyPlanState();
  if (token) {
    pollNow();
    schedule();
  } else {
    pollNow();
  }
})();
