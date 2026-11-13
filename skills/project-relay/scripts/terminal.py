"""Read-only terminal view for project relay.

The view reuses dashboard.build_state, so it only reads the project's SQLite
database in read-only mode and the per-attempt live JSONL files. It never
starts HTTP, creates task state, or calls a model. Summary rules are adapted
from simple10/agents-observe (MIT); see skills/project-relay/third_party/.
"""
import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from dashboard import StateError, _clean_env, build_state

LINE_LIMIT = 600
HISTORY_LIMIT = 2000
ANSI_ESCAPE = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")
CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
KIND_LABELS = {
    "started": "开始",
    "system": "系统",
    "tool_use": "工具",
    "tool_result": "结果",
    "assistant": "回复",
    "assistant_delta": "输出",
    "stderr": "错误输出",
    "result": "最终结果",
    "finished": "结束",
    "parse_error": "解析失败",
    "error": "错误",
    "event": "事件",
}
REVIEW_LABELS = {
    "review_passed": "阶段复核通过（待最终验收）",
    "review_changes_requested": "要求返工",
    "approved": "最终验收通过",
}


def configure_console():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


def one_line(value):
    text = "" if value is None else str(value)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^[-*] ", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*\n\s*", " ", text)
    return text.strip()


def shorten(value, limit=LINE_LIMIT):
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def relative_path(file, cwd):
    if not file:
        return ""
    path = str(file).replace("\\", "/")
    if not cwd:
        return path
    root = str(cwd).replace("\\", "/").rstrip("/")
    if not root:
        return path
    if path.lower() == root.lower():
        return "."
    if path.lower().startswith(root.lower() + "/"):
        return path[len(root) + 1:]
    return path


VALID_BINARY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
SHELL_WORDS = {"for", "do", "done", "if", "then", "else", "fi", "while", "case", "esac"}


def extract_bash_binary(command):
    first = str(command or "").split("\n", 1)[0].strip()
    skip_next = False
    for token in first.split():
        if skip_next:
            skip_next = False
            continue
        if "=" in token or token in ("&&", ";", "||"):
            continue
        if token.startswith("$(") or token.startswith("`"):
            continue
        if token == "cd":
            skip_next = True
            continue
        if token in SHELL_WORDS:
            continue
        binary = re.split(r"[\\/]", token)[-1]
        if binary and VALID_BINARY_RE.match(binary):
            return binary
    return None


def tool_summary(tool_name, tool_input, cwd):
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash":
        description = tool_input.get("description")
        command = tool_input.get("command")
        binary = extract_bash_binary(command) if command else None
        prefix = f"[{binary}] " if binary else ""
        if description:
            return prefix + one_line(description)
        return prefix + one_line(command) if command else ""
    if tool_name in ("Read", "Write", "Edit"):
        return relative_path(tool_input.get("file_path"), cwd)
    if tool_name == "Grep":
        pattern = tool_input.get("pattern")
        path = relative_path(tool_input.get("path"), cwd) if tool_input.get("path") else ""
        if pattern and path:
            return f"/{pattern}/ in {path}"
        return f"/{pattern}/" if pattern else ""
    if tool_name == "Glob":
        return str(tool_input.get("pattern") or "")
    if tool_name == "Agent":
        return one_line(tool_input.get("description") or tool_input.get("prompt") or "")
    if tool_name == "Skill":
        return str(tool_input.get("skill") or "")
    if tool_name in ("WebSearch", "WebFetch"):
        return str(tool_input.get("query") or tool_input.get("url") or "")
    if tool_name == "NotebookEdit":
        return relative_path(tool_input.get("notebook_path"), cwd)
    return one_line(tool_input.get("description") or tool_input.get("command")
                    or tool_input.get("query") or "")


def tool_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return ""


def format_time(value):
    if not value:
        return "--:--:--"
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is not None:
            moment = moment.astimezone()
        return moment.strftime("%H:%M:%S")
    except ValueError:
        return str(value)[:19]


def format_datetime(value):
    if not value:
        return "时间未记录"
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is not None:
            moment = moment.astimezone()
        return moment.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value)


def short_id(value):
    text = str(value or "")
    return text[:8] if len(text) > 8 else text


def is_noise(event):
    kind = event.get("kind")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if kind in ("thinking", "thinking_delta"):
        return True
    if payload.get("subtype") in ("thinking", "thinking_tokens"):
        return True
    if payload.get("type") in ("thinking", "thinking_delta"):
        return True
    return False


def event_summary(event, project_root):
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    kind = event.get("kind")
    if kind == "started":
        model = payload.get("model") or "模型未记录"
        return f"第 {payload.get('attempt_number') or '?'} 次执行开始 · {model}"
    if kind == "system":
        subtype = payload.get("subtype") or ""
        model = f" · {payload['model']}" if payload.get("model") else ""
        session = f" · 会话 {short_id(payload['session_id'])}" if payload.get("session_id") else ""
        if subtype == "init":
            return f"会话初始化{model}{session}"
        if subtype == "status":
            state = f" · {one_line(payload['status'])}" if payload.get("status") else ""
            return f"状态{state}{session}"
        if subtype == "task_summary":
            summary = f" · {one_line(payload['summary'])}" if payload.get("summary") else ""
            return f"任务摘要{summary}{session}"
        return f"系统{subtype and ' · ' + subtype or ''}{session}"
    if kind == "tool_use":
        detail = tool_summary(payload.get("name"), payload.get("input"), project_root)
        return f"{payload.get('name') or '工具'}：{detail}" if detail else (payload.get("name") or "工具调用")
    if kind == "tool_result":
        detail = one_line(tool_result_text(payload.get("content")))
        prefix = "工具失败" if payload.get("is_error") else "工具结果"
        return f"{prefix}：{detail}" if detail else prefix
    if kind == "assistant":
        return one_line(payload.get("text")) or "…"
    if kind == "assistant_delta":
        return one_line(payload.get("text")) or "…"
    if kind == "stderr":
        return one_line(payload.get("text")) or "错误输出"
    if kind == "result":
        detail = one_line(payload.get("result") or payload.get("error"))
        subtype = f" · {payload['subtype']}" if payload.get("subtype") else ""
        return f"最终结果{subtype}：{detail}" if detail else f"最终结果{subtype}"
    if kind == "finished":
        return "执行结束" if payload.get("ok") else (
            f"执行结束（失败）：{one_line(payload.get('error') or '未知原因')}")
    if kind == "parse_error":
        return f"无法解析的输出：{one_line(payload.get('text'))}"
    if kind == "error":
        return one_line(payload.get("text") or payload.get("error")) or "错误"
    if kind == "event":
        return " · ".join(str(value) for value in (payload.get("type"), payload.get("subtype"))
                          if value) or "事件"
    return one_line(payload.get("text") or payload.get("message")) or (kind or "事件")


def format_event(event, project_root):
    label = KIND_LABELS.get(event.get("kind"), event.get("kind") or "事件")
    summary = shorten(one_line(event_summary(event, project_root)), LINE_LIMIT)
    return f"[{format_time(event.get('at'))}] {label}：{summary or '…'}"


@dataclass(frozen=True)
class TextFragment:
    text: str


def emit(lines):
    if not lines:
        return
    for line in lines:
        text = line.text if isinstance(line, TextFragment) else line + "\n"
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = CONTROL_CHARACTER.sub("", ANSI_ESCAPE.sub("", text))
        sys.stdout.write(text)
    sys.stdout.flush()


class TerminalView:
    def __init__(self, project, task):
        self.project = str(Path(project).resolve())
        self.task = task
        self.state = None
        self.attempts = {}
        self.seen_reviews = set()
        self.seen_checkpoints = set()
        self.last_status = None
        self.stream_attempt = None

    def poll(self):
        state = build_state(self.project, task=self.task)
        lines = []
        if self.state is None:
            lines.extend(self._header_lines(state))
            lines.extend(self._plan_lines(state))
            lines.extend(self._history_lines(state, initial=True))
            lines.extend(self._attempt_lines(state, initial=True))
        else:
            lines.extend(self._status_lines(state))
            lines.extend(self._history_lines(state, initial=False))
            if lines:
                lines = self.close_stream() + lines
            lines.extend(self._attempt_lines(state, initial=False))
        self.state = state
        return lines

    def close_stream(self):
        if self.stream_attempt is None:
            return []
        self.stream_attempt = None
        return [TextFragment("\n")]

    def _stream_text(self, attempt_id, text, at):
        lines = []
        if self.stream_attempt != attempt_id:
            lines.extend(self.close_stream())
            lines.append(TextFragment(f"[{format_time(at)}] 输出："))
            self.stream_attempt = attempt_id
        lines.append(TextFragment(text))
        return lines

    def _header_lines(self, state):
        task = state["task"]
        self.last_status = (state["stage"], state["status_label"], state["status_detail"])
        return [
            "=" * 64,
            f"接力终端看板 · {task['title']}",
            f"任务 {task['id']} · 负责人 {task.get('owner') or '未记录'} · "
            f"分支 {task.get('branch') or '无'}",
            f"阶段：{state.get('status_label') or task['status']} — "
            f"{state.get('status_detail') or '等待派活'}",
            f"最后事件：{format_datetime(state.get('last_event_at'))}",
            "=" * 64,
        ]

    def _plan_lines(self, state):
        return ["", "── 任务目标与验收 ──", state.get("plan") or "（任务没有计划正文）"]

    def _history_lines(self, state, initial):
        reviews = state.get("reviews") or []
        checkpoints = state.get("checkpoints") or []
        new_reviews = [item for item in reviews
                       if item.get("sequence") not in self.seen_reviews]
        new_checkpoints = [item for item in checkpoints
                           if item.get("sequence") not in self.seen_checkpoints]
        if not initial and not new_reviews and not new_checkpoints:
            return []
        lines = ["", "── Codex 审核与返工 ──"]
        if not reviews:
            lines.append("尚无复核记录。")
        else:
            for item in (new_reviews if not initial else reviews):
                lines.extend(self._history_item(item, "review"))
        lines.extend(["", "── 检查点 ──"])
        if not checkpoints:
            lines.append("尚无检查点。")
        else:
            for item in (new_checkpoints if not initial else checkpoints):
                lines.extend(self._history_item(item, "checkpoint"))
        self.seen_reviews.update(item.get("sequence") for item in reviews)
        self.seen_checkpoints.update(item.get("sequence") for item in checkpoints)
        return lines

    def _history_item(self, item, kind):
        label = (REVIEW_LABELS.get(item.get("kind"), item.get("kind"))
                 if kind == "review" else "阶段检查点")
        lines = ["", f"[{format_datetime(item.get('at'))}] {label} · "
                     f"{item.get('agent') or '未记录'}"]
        body = item.get("body") or "（无正文）"
        truncated = len(body) > HISTORY_LIMIT
        body = shorten(body, HISTORY_LIMIT)
        for line in body.splitlines() or [""]:
            lines.append("  " + line)
        if truncated:
            lines.append(f"  …（已截断；完整记录可用 handoff --task {self.task} 查看）")
        return lines

    def _status_lines(self, state):
        status = (state["stage"], state["status_label"], state["status_detail"])
        if status == self.last_status:
            return []
        self.last_status = status
        return ["", f"【状态更新】{state['status_label']} — "
                    f"{state.get('status_detail') or ''}".rstrip(" —")]

    def _attempt_lines(self, state, initial):
        attempts = state.get("attempts") or []
        if not attempts:
            return ["", "── 执行批次 ──", "尚无执行批次。"] if initial else []
        lines = []
        for attempt in attempts:
            lines.extend(self._one_attempt(attempt))
        return lines

    def _one_attempt(self, attempt):
        attempt_id = attempt.get("id")
        file_name = attempt.get("file") or ""
        log_path = Path(self.project) / "协作记录" / self.task / file_name
        offset = int(attempt.get("event_offset") or 0)
        total = int(attempt.get("event_total") or 0)
        events = attempt.get("events") or []
        lines = []
        info = self.attempts.get(attempt_id)
        if info is None:
            lines.append("")
            lines.append(f"── 第 {attempt.get('number')} 次执行 · {attempt_id} ──")
            lines.append(f"开始：{format_datetime(attempt.get('started_at'))} · "
                         f"原始日志：{log_path}")
            if attempt.get("ended"):
                lines.append("本批次已结束。" if attempt.get("end_ok")
                             else f"本批次失败：{attempt.get('end_error') or '未知原因'}")
            else:
                lines.append("本批次进行中；视图持续刷新。")
            if attempt.get("truncated"):
                lines.append(f"仅显示最近 {len(events)} 条（共 {total} 条，"
                             f"窗口偏移 {offset}）；完整日志见上。")
            if attempt.get("incomplete_tail"):
                lines.append("最后一行尚未写完整，已忽略；完整日志见上。")
            if attempt.get("parse_errors"):
                lines.append(f"{attempt['parse_errors']} 行无法解析，已保留原日志。")
            info = {"next_abs": offset, "streamed": "", "skip_delta": "", "last_assistant": ""}
            self.attempts[attempt_id] = info
        else:
            cursor = info["next_abs"]
            if offset > cursor:
                lines.append(f"（日志窗口前移 {offset - cursor} 条；早期内容已不在当前窗口，"
                             f"完整日志：{log_path}）")
                info.update(next_abs=offset, streamed="", skip_delta="", last_assistant="")
            elif offset + len(events) < cursor:
                lines.append(f"（日志窗口回退，重新显示当前窗口；完整日志：{log_path}）")
                info.update(next_abs=offset, streamed="", skip_delta="", last_assistant="")
        cursor = info["next_abs"]
        if lines:
            lines = self.close_stream() + lines
        for index, event in enumerate(events):
            absolute = offset + index
            if absolute < cursor or is_noise(event):
                continue
            if event.get("kind") == "assistant_delta":
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                text = str(payload.get("text") or "")
                skipped = info["skip_delta"]
                if skipped:
                    matched = min(len(text), len(skipped))
                    if text[:matched] == skipped[:matched]:
                        info["skip_delta"] = skipped[matched:]
                        text = text[matched:]
                    else:
                        info["skip_delta"] = ""
                if text:
                    lines.extend(self._stream_text(attempt_id, text, event.get("at")))
                    info["streamed"] += text
                continue
            if event.get("kind") == "assistant":
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                text = str(payload.get("text") or "")
                streamed = info["streamed"]
                info["streamed"] = ""
                info["last_assistant"] = text
                if streamed and text.startswith(streamed):
                    remaining = text[len(streamed):]
                    if remaining:
                        lines.extend(self._stream_text(attempt_id, remaining, event.get("at")))
                    info["skip_delta"] += remaining
                    lines.extend(self.close_stream())
                    continue
                info["skip_delta"] += text
            if event.get("kind") == "result":
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                if info["last_assistant"] and payload.get("result") == info["last_assistant"]:
                    event = {**event, "payload": {**payload, "result": ""}}
            lines.extend(self.close_stream())
            lines.append(format_event(event, self.project))
        info["next_abs"] = max(cursor, offset + len(events))
        return lines


def validate_interval(value):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("刷新间隔必须为正数且为有限值")
    return float(value)


def write_ready(path, payload):
    path = Path(path)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def follow(project, task, interval, ready_file=None):
    view = TerminalView(project, task)
    emit(view.poll())
    if ready_file:
        write_ready(ready_file, {"ok": True, "pid": os.getpid(), "task": task,
                                 "project": str(Path(project).resolve())})
    while True:
        time.sleep(interval)
        try:
            emit(view.poll())
        except (StateError, OSError, sqlite3.Error) as exc:
            emit([f"（读取失败：{exc}；将继续重试）"])


def monitor_text(args, project):
    try:
        interval = validate_interval(args.interval)
        if getattr(args, "open", False):
            raise ValueError("--open 由 JSON 控制命令处理")
        if args.once:
            view = TerminalView(project, args.task)
            emit(view.poll() + view.close_stream())
            return 0
        return follow(project, args.task, interval, getattr(args, "ready_file", None))
    except KeyboardInterrupt:
        emit(["", "已退出查看，不影响执行。"])
        return 0
    except (ValueError, StateError, OSError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr, flush=True)
        return 1


def _viewer_command(project, task, interval, ready_file):
    return [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
            "--project", str(Path(project).resolve()), "--task", task,
            "--interval", repr(float(interval)), "--ready-file", str(ready_file)]


def _terminate(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def open_window(project, task, interval=1.0):
    """Open an independent console that follows the task.

    The viewer is a direct argument list, uses CREATE_NEW_CONSOLE on Windows,
    inherits a cleaned environment without model tokens, and is not assigned
    to the relay worker's Job Object. Readiness is confirmed by the viewer
    itself before a pid is returned.
    """
    project = Path(project).resolve()
    interval = validate_interval(interval)
    try:
        build_state(project, task=task)
    except StateError as exc:
        raise ValueError(str(exc)) from exc
    ready_file = Path(tempfile.gettempdir()) / f"project-relay-terminal-{uuid4().hex}.ready"
    command = _viewer_command(project, task, interval, ready_file)
    environment = _clean_env()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    if os.name != "nt":
        raise ValueError("独立终端窗口仅支持 Windows；可在当前终端运行 monitor --task")
    try:
        child = subprocess.Popen(command, cwd=str(project), env=environment,
                                 creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)
    except OSError as exc:
        raise ValueError(f"终端窗口启动失败：无法启动查看进程：{exc}") from exc
    deadline = time.monotonic() + 10
    payload = None
    while time.monotonic() < deadline:
        if child.poll() is not None:
            break
        try:
            payload = json.loads(ready_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            if payload.get("ok") is True and payload.get("pid") == child.pid:
                if payload.get("task") == task:
                    break
            if payload.get("ok") is False:
                break
        time.sleep(0.05)
    try:
        ready_file.unlink()
    except OSError:
        pass
    if (isinstance(payload, dict) and payload.get("ok") is True
            and payload.get("pid") == child.pid and payload.get("task") == task):
        return {"opened": True, "pid": child.pid, "task": task,
                "interval": interval, "ready": True}
    if child.poll() is None:
        _terminate(child.pid)
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    if isinstance(payload, dict) and payload.get("ok") is False:
        raise ValueError(f"终端窗口启动失败：{payload.get('error') or '查看进程未就绪'}")
    raise ValueError("终端窗口启动失败：查看进程未在时限内就绪；"
                     "可在当前终端运行 monitor --task 排查")



def monitor_command(args, project):
    interval = validate_interval(args.interval)
    if args.once:
        raise ValueError("--open 不能与 --once 同时使用")
    return open_window(project, task=args.task, interval=interval)


def main():
    configure_console()
    parser = argparse.ArgumentParser(description="项目接力只读终端视图")
    parser.add_argument("--project", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--ready-file", type=Path)
    args = parser.parse_args()
    return monitor_text(args, Path(args.project).resolve())


if __name__ == "__main__":
    sys.exit(main())
