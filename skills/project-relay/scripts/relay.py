"""Local project handoff. Python standard library only; no model API client."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlparse
from process_tree import ProcessTree, process_alive

RECORDS = "协作记录"
DEFAULT_CONFIG = Path.home() / ".project-relay" / "config.json"
REDACTED = "[REDACTED]"
PLAN_LIMIT = 12000
CHECKPOINT_LIMIT = 3000
REVIEW_LIMIT = 3000
EXECUTION_ERROR_LIMIT = 500
CHANGES_LIMIT = 1000
CONTEXT_LIMIT = 20000
REVIEW_KINDS = ("review_passed", "review_changes_requested", "approved")
PROMPT_PREFIX = ("你是本机项目执行负责人，实际模型由 CC Switch 路由决定。使用简体中文。\n"
                 "执行下面计划，遵守用户确认的范围、项目规范与验收条件。"
                 "完成后报告修改、实际测试、未验证事项、下一步。\n"
                 "按阶段保存进度；你的完成结果进入待 Codex 复核。"
                 "只执行本任务，不自动提交、推送、部署。\n")


def now():
    return datetime.now(timezone.utc).isoformat()


def redact_text(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def redact_value(value, secrets):
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {redact_text(str(key), secrets): redact_value(item, secrets)
                for key, item in value.items()}
    return value


class DeltaRedactor:
    """Replace a secret even when it arrives split across delta events.

    The tail of a bounded buffer is held back until enough context exists to
    decide whether a secret straddles the chunk boundary.
    """

    def __init__(self, secrets):
        self.secrets = [secret for secret in secrets if isinstance(secret, str) and secret]
        self.keep = max((len(secret) for secret in self.secrets), default=1) - 1
        self.buffer = ""

    def feed(self, text, final=False):
        self.buffer += text
        redacted = redact_text(self.buffer, self.secrets)
        if final or self.keep <= 0:
            self.buffer = ""
            return redacted
        if len(redacted) <= self.keep:
            self.buffer = redacted
            return ""
        self.buffer = redacted[-self.keep:]
        return redacted[:-self.keep]


class LiveLog:
    def __init__(self, path, attempt_id, secrets):
        self.path = path
        self.attempt_id = attempt_id
        self.secrets = secrets
        self.session_id = None
        self.handle = open(path, "a", encoding="utf-8")

    def write(self, kind, payload):
        entry = {"at": now(), "attempt_id": self.attempt_id, "kind": kind,
                 "payload": payload}
        if self.session_id:
            entry["session_id"] = self.session_id
        line = json.dumps(redact_value(entry, self.secrets), ensure_ascii=False)
        self.handle.write(redact_text(line, self.secrets) + "\n")
        self.handle.flush()

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def read_text(path):
    text = Path(path).read_text(encoding="utf-8-sig").strip()
    if not text:
        raise ValueError("正文不能为空")
    return text


def git(project, *args):
    result = subprocess.run(["git", "-C", str(project), *args], capture_output=True,
                            encoding="utf-8", errors="replace")
    return result.stdout.strip() if result.returncode == 0 else None


def identity(project):
    return {"branch": git(project, "symbolic-ref", "--short", "HEAD"),
            "head": git(project, "rev-parse", "--verify", "HEAD")}


def connect(project, initialize=False):
    folder = project / RECORDS
    if not project.is_dir():
        raise ValueError("项目目录不存在")
    if not initialize and not (folder / "state.sqlite3").exists():
        raise ValueError("项目尚未初始化，请先运行 init")
    folder.mkdir(exist_ok=True)
    db = sqlite3.connect(folder / "state.sqlite3", timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(root TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, title TEXT NOT NULL, plan TEXT NOT NULL,
          owner TEXT NOT NULL, status TEXT NOT NULL, branch TEXT,
          start_sha TEXT, reviewed_sha TEXT, run_pid INTEGER, child_pid INTEGER);
        CREATE TABLE IF NOT EXISTS events(
          sequence INTEGER PRIMARY KEY, task TEXT NOT NULL, at TEXT NOT NULL,
          agent TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reviews(branch TEXT PRIMARY KEY, sha TEXT NOT NULL);
    """)
    root = db.execute("SELECT root FROM meta").fetchone()
    if root is None:
        db.execute("INSERT INTO meta VALUES (?)", (str(project),))
        db.commit()
    elif os.path.normcase(root["root"]) != os.path.normcase(str(project)):
        db.close()
        raise ValueError("记录属于另一项目路径；请核对原项目，不能静默迁移")
    return db


def task_row(db, task, project=None):
    row = db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
    if row is None:
        raise ValueError("任务不存在")
    task = dict(row)
    if project is not None and task["branch"] != identity(project)["branch"]:
        raise ValueError("当前分支与任务分支不同，请切回原分支")
    return task


def event(db, task, agent, kind, body):
    db.execute("INSERT INTO events(task,at,agent,kind,body) VALUES (?,?,?,?,?)",
               (task, now(), agent, kind, body))


def summary(db):
    return [dict(r) for r in db.execute("SELECT * FROM tasks ORDER BY rowid")]


def index(project, db):
    lines = ["# 开发接力索引", "", "状态以 state.sqlite3 为准；任务正文与历史可用 handoff 命令读取。", ""]
    for task in summary(db):
        lines.append(f'- {task["id"]}：{task["title"]}；负责人 {task["owner"]}；{task["status"]}')
    target = project / RECORDS / "README.md"
    temp = target.with_name(f".index-{uuid.uuid4().hex}.tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp.replace(target)


def review_baseline(db, task):
    branch = task["branch"] or "(detached-or-no-git)"
    reviewed = db.execute("SELECT sha FROM reviews WHERE branch=?", (branch,)).fetchone()
    first = db.execute("SELECT start_sha FROM tasks WHERE branch IS ? AND start_sha IS NOT NULL ORDER BY rowid LIMIT 1",
                       (task["branch"],)).fetchone()
    return reviewed["sha"] if reviewed else (first["start_sha"] if first else None)


def review_state(db, project, task):
    current = identity(project)
    baseline = review_baseline(db, task)
    reviewed = db.execute("SELECT sha FROM reviews WHERE branch=?",
                          (task["branch"] or "(detached-or-no-git)",)).fetchone()
    if baseline and git(project, "merge-base", "--is-ancestor", baseline,
                        current["head"] or "HEAD") is None:
        raise ValueError("审核起点不在当前历史中；请核对分支或历史重写，不能跳过待复核提交")
    working = git(project, "status", "--short", "--", ".", f":(exclude){RECORDS}/**")
    return {
        "git": current,
        "review_from": baseline,
        "changes": git(project, "diff", "--stat", baseline, "--", ".",
                       f":(exclude){RECORDS}/**") if baseline else None,
        "working_tree": working,
        "last_reviewed_sha": reviewed["sha"] if reviewed else None,
        "needs_review": not reviewed or current["head"] != reviewed["sha"] or bool(working),
    }


def handoff(db, project, task_id):
    task = task_row(db, task_id, project)
    events = [dict(r) for r in db.execute("SELECT * FROM events WHERE task=? ORDER BY sequence", (task_id,))]
    result = {"project": str(project), "task": task, "events": events,
              "checkpoints": [r for r in events if r["kind"] == "checkpoint"]}
    result.update(review_state(db, project, task))
    return result


def limited(text, limit, note):
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text, False
    marker = f"\n…（已截断至 {limit} 字符；{note}）"
    return text[:max(0, limit - len(marker))] + marker, True


def _latest_event(db, task_id, kinds):
    placeholders = ",".join("?" for _ in kinds)
    return db.execute(f"SELECT * FROM events WHERE task=? AND kind IN ({placeholders}) "
                      "ORDER BY sequence DESC LIMIT 1", (task_id, *kinds)).fetchone()


def build_brief(db, project, task_id):
    task = task_row(db, task_id, project)
    plan = task["plan"] or ""
    if len(plan) > PLAN_LIMIT:
        raise ValueError(f"计划超过 {PLAN_LIMIT} 字符（当前 {len(plan)} 字符），"
                         "请先整理计划；不会静默截断验收条件")
    review = review_state(db, project, task)

    last_checkpoint = None
    row = _latest_event(db, task_id, ("checkpoint",))
    if row is not None:
        body, truncated = limited(row["body"], CHECKPOINT_LIMIT,
                                  f"完整检查点可用 handoff --task {task_id} 查看")
        last_checkpoint = {"at": row["at"], "agent": row["agent"],
                           "body": body, "truncated": truncated}

    last_review = None
    row = _latest_event(db, task_id, REVIEW_KINDS)
    if row is not None:
        body, truncated = limited(row["body"], REVIEW_LIMIT,
                                  f"完整意见可用 handoff --task {task_id} 查看")
        last_review = {"at": row["at"], "agent": row["agent"], "kind": row["kind"],
                       "body": body, "truncated": truncated}

    last_execution = None
    row = _latest_event(db, task_id, ("execution",))
    if row is not None:
        try:
            payload = json.loads(row["body"])
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        attempt_id = payload.get("attempt_id")
        evidence = None
        if isinstance(attempt_id, str):
            candidate = project / RECORDS / task_id / f"执行-{attempt_id}.json"
            if candidate.is_file():
                evidence = str(candidate)
        error, truncated = limited(payload.get("error"), EXECUTION_ERROR_LIMIT,
                                   f"完整错误见证据文件 {evidence or '（缺失）'}")
        last_execution = {"ok": bool(payload.get("ok")), "error": error or None,
                          "truncated": truncated,
                          "finished_at": payload.get("finished_at") or row["at"],
                          "attempt_id": attempt_id, "evidence": evidence}

    changes, changes_truncated = limited(review["changes"], CHANGES_LIMIT,
                                         "完整改动可用 Git 查看")
    return {
        "project": str(project),
        "task": {key: task[key] for key in ("id", "title", "plan", "owner", "status",
                                            "branch", "start_sha")},
        "git": {"branch": review["git"]["branch"], "head": review["git"]["head"]},
        "review_from": review["review_from"],
        "last_reviewed_sha": review["last_reviewed_sha"],
        "needs_review": review["needs_review"],
        "changes": changes or None,
        "changes_truncated": changes_truncated,
        "last_checkpoint": last_checkpoint,
        "last_review": last_review,
        "last_execution": last_execution,
    }


def serialize_brief(context):
    text = json.dumps(context, ensure_ascii=False)
    if len(text) > CONTEXT_LIMIT:
        raise ValueError(f"派活上下文超过 {CONTEXT_LIMIT} 字符（当前 {len(text)} 字符），"
                         "请精简计划、检查点、审核意见或改动概况后重新派活")
    return text


def brief_handoff(db, project, task_id):
    context = build_brief(db, project, task_id)
    text = serialize_brief(context)
    return {"brief": context, "context_chars": len(text), "context_limit": CONTEXT_LIMIT}


def load_executor(path):
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    settings = json.loads(Path(config["desktop_config"]).read_text(encoding="utf-8-sig"))
    state = json.loads(Path(config["cc_switch_state"]).read_text(encoding="utf-8-sig"))
    route = state.get("apps", {}).get("claude-desktop", {})
    if (route.get("mode") != "proxy" or not route.get("attached") or
            route.get("proxy_route") != config["expected_provider_id"]):
        raise ValueError("CC Switch 当前桌面路由与已验证 DeepSeek 提供商不一致")
    if config.get("desktop_meta"):
        meta = json.loads(Path(config["desktop_meta"]).read_text(encoding="utf-8-sig"))
        if meta.get("appliedId") != Path(config["desktop_config"]).stem:
            raise ValueError("桌面已应用配置发生变化，请重新核对执行器配置")
    endpoint = settings.get("inferenceGatewayBaseUrl", "")
    address = urlparse(endpoint)
    if address.scheme != "http" or address.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("执行器只接受已验证的本机 CC Switch 代理")
    key = settings.get("inferenceGatewayApiKey")
    if config.get("cc_switch_db"):
        try:
            gateway = sqlite3.connect(Path(config["cc_switch_db"]).resolve().as_uri() + "?mode=ro",
                                      uri=True, timeout=5)
            try:
                row = gateway.execute("SELECT value FROM settings WHERE key=?",
                                      ("claude_desktop_gateway_token",)).fetchone()
            finally:
                gateway.close()
        except sqlite3.Error as exc:
            raise ValueError("无法只读获取 CC Switch 当前网关凭据，请检查 cc_switch_db") from exc
        key = row[0] if row else None
        if not isinstance(key, str) or not key.strip():
            raise ValueError("CC Switch 当前网关凭据缺失，不回退到桌面旧凭据")
    if not key or not isinstance(key, str):
        raise ValueError("桌面代理凭据缺失")
    models = {m["name"]: m.get("labelOverride") for m in settings.get("inferenceModels", [])}
    if models.get(config["model"]) != config["expected_upstream_model"]:
        raise ValueError("桌面模型映射发生变化，请核对 DeepSeek 实际模型")
    command = config["command"]
    if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
        raise ValueError("command 必须为可执行文件及参数数组")
    if not Path(command[0]).is_file():
        raise ValueError("执行器文件不存在，请核对桌面 Claude Code 版本路径")
    env = os.environ.copy()
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE"):
        env.pop(name, None)
    env.update(ANTHROPIC_BASE_URL=endpoint, ANTHROPIC_AUTH_TOKEN=key,
               ANTHROPIC_MODEL=config["model"], CLAUDE_CODE_ENTRYPOINT="claude-desktop-3p",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    metadata = {"command": command, "endpoint": endpoint, "model": config["model"],
                "upstream_model": models[config["model"]], "provider_id": route["proxy_route"],
                "credential_present": True}
    return config, env, metadata


def ensure_idle(db):
    for row in db.execute("SELECT * FROM tasks WHERE run_pid IS NOT NULL").fetchall():
        if process_alive(row["run_pid"]) or process_alive(row["child_pid"]):
            raise ValueError("项目仍有自动执行进程；请先等待该进程结束，再接管或派活")
        event(db, row["id"], "system", "interrupted", "控制进程和执行进程均已结束；保留最近检查点，待复核文件")
        db.execute("UPDATE tasks SET run_pid=NULL,child_pid=NULL,status='interrupted' WHERE id=?", (row["id"],))


def stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        process.kill()


def record_rejection(project, db, task_id, error):
    folder = project / RECORDS / task_id
    folder.mkdir(exist_ok=True)
    payload = {"at": now(), "task": task_id, "agent": "claude", "ok": False,
               "error": str(error), "executor_started": False}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path = folder / f"执行拒绝-{uuid.uuid4().hex[:12]}.json"
    path.write_text(text, encoding="utf-8")
    with db:
        event(db, task_id, "claude", "execution_rejected", text)
    index(project, db)
    return path


def run_task(args, project, db):
    task_row(db, args.task)
    try:
        brief_text = serialize_brief(build_brief(db, project, args.task))
    except (ValueError, sqlite3.Error) as exc:
        record_rejection(project, db, args.task, exc)
        raise
    try:
        config, env, metadata = load_executor(args.config)
    except (ValueError, OSError, KeyError) as exc:
        record_rejection(project, db, args.task, exc)
        raise
    folder = project / RECORDS / args.task
    folder.mkdir(exist_ok=True)
    secrets = [env.get("ANTHROPIC_AUTH_TOKEN")]
    live = None
    live_path = None
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            ensure_idle(db)
            task = task_row(db, args.task, project)
            attempt_number = len(list(folder.glob("live-*.jsonl"))) + 1
            attempt_id = uuid.uuid4().hex[:12]
            live_path = folder / f"live-{attempt_id}.jsonl"
            live = LiveLog(live_path, attempt_id, secrets)
            event(db, args.task, "claude", "dispatched", json.dumps(metadata, ensure_ascii=False))
            event(db, args.task, "claude", "run_started", json.dumps(
                {"attempt_id": attempt_id, "attempt_number": attempt_number}, ensure_ascii=False))
            db.execute("UPDATE tasks SET owner='claude',status='running',run_pid=?,child_pid=NULL WHERE id=?",
                       (os.getpid(), args.task))
    except BaseException:
        if live is not None:
            live.close()
            try:
                if live_path.stat().st_size == 0:
                    live_path.unlink()
            except OSError:
                pass
        raise
    evidence = folder / f"执行-{attempt_id}.json"
    prompt = PROMPT_PREFIX + brief_text
    command = config["command"] + ["--print", "--output-format", "stream-json", "--verbose",
        "--include-partial-messages", "--model", config["model"],
        "--permission-mode", "acceptEdits", "--allowedTools", "Read,Edit,Write,Bash,Glob,Grep",
        "--max-turns", str(args.max_turns)]
    result = {"started_at": now(), "route": metadata, "ok": False, "error": "执行尚未结束",
              "attempt_id": attempt_id, "attempt_number": attempt_number,
              "session_id": None, "live": str(live_path),
              "context_chars": len(brief_text), "context_limit": CONTEXT_LIMIT}
    dashboard_url = None
    if config.get("dashboard") is True:
        try:
            from dashboard import start as start_dashboard
            dashboard_url = start_dashboard(project, task=args.task, open_browser=True)["url"]
        except (ValueError, OSError, sqlite3.Error) as exc:
            result["dashboard_error"] = f"看板启动失败：{exc}"
    evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    live.write("started", {"attempt_number": attempt_number, "model": metadata["model"]})
    process = None
    tree = None
    timed_out = False
    returncode = None
    final_payload = None
    parse_errors = 0
    session_id = None
    worker_error = None
    control_error = None
    stdout_parts = []
    stderr_parts = []
    delta_redactor = DeltaRedactor(secrets)
    stderr_redactor = DeltaRedactor(secrets)

    def handle_stderr(text):
        safe = stderr_redactor.feed(text)
        if safe:
            stderr_parts.append(safe)
            live.write("stderr", {"text": safe})

    def append_evidence(payload):
        stdout_parts.append(json.dumps(redact_value(payload, secrets), ensure_ascii=False) + "\n")

    def handle_stdout_line(line):
        nonlocal final_payload, parse_errors, session_id
        stripped = line.strip()
        if not stripped:
            return
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            parse_errors += 1
            live.write("parse_error", {"text": stripped})
            append_evidence({"type": "parse_error", "text": stripped})
            return
        if not isinstance(payload, dict):
            live.write("event", {"value": payload})
            append_evidence({"type": "event", "value": payload})
            return
        kind = payload.get("type")
        if kind is None:
            final_payload = payload
            live.write("result", payload)
            append_evidence(payload)
            return
        reported_session = payload.get("session_id")
        if isinstance(reported_session, str) and reported_session:
            session_id = reported_session
            live.session_id = reported_session
        if kind == "system":
            subtype = payload.get("subtype")
            if subtype == "thinking_tokens":
                # Legitimate per-minute token statistics, not an operation.
                return
            summary = {"type": "system", "subtype": subtype,
                       "session_id": payload.get("session_id"), "model": payload.get("model")}
            for field in ("status", "summary"):
                if payload.get(field) is not None:
                    summary[field] = payload.get(field)
            live.write("system", summary)
            append_evidence(summary)
        elif kind == "assistant":
            message = payload.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            published = []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    live.write("assistant", {"text": block.get("text", "")})
                    published.append(block)
                elif block_type == "tool_use":
                    live.write("tool_use", {"id": block.get("id"), "name": block.get("name"),
                                            "input": block.get("input")})
                    published.append(block)
                # Thinking blocks are deliberately not published.
            append_evidence({"type": "assistant", "message": {"content": published}})
        elif kind == "user":
            message = payload.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            published = []
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    live.write("tool_result", {"tool_use_id": block.get("tool_use_id"),
                                               "is_error": block.get("is_error", False),
                                               "content": block.get("content")})
                    published.append(block)
            append_evidence({"type": "user", "message": {"content": published}})
        elif kind == "result":
            final_payload = payload
            live.write("result", payload)
            append_evidence(payload)
        elif kind == "stream_event":
            streamed = payload.get("event")
            if isinstance(streamed, dict) and streamed.get("type") == "content_block_delta":
                delta = streamed.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    safe = delta_redactor.feed(str(delta.get("text", "")))
                    if safe:
                        live.write("assistant_delta", {"text": safe})
                        append_evidence({"type": "assistant_delta", "text": safe})
                # Thinking deltas are deliberately not published.
        elif kind == "system_error":
            worker_error = str(payload.get("error") or payload.get("message") or "系统错误")
            live.write("error", {"text": worker_error})
            append_evidence({"type": "error", "text": worker_error})
        else:
            summary = {"type": kind, "subtype": payload.get("subtype")}
            live.write("event", summary)
            append_evidence(summary)

    def handle_worker_line(line):
        nonlocal returncode, worker_error
        stripped = line.strip()
        if not stripped:
            return False
        try:
            envelope = json.loads(stripped)
        except json.JSONDecodeError:
            handle_stderr(line)
            return False
        if not isinstance(envelope, dict):
            handle_stderr(line)
            return False
        kind = envelope.get("kind")
        if kind == "stdout":
            handle_stdout_line(envelope.get("line", ""))
        elif kind == "stderr":
            handle_stderr(envelope.get("line", ""))
        elif kind == "worker_error":
            worker_error = str(envelope.get("error") or "执行器读取失败")
            live.write("error", {"text": worker_error})
        elif kind == "exit":
            value = envelope.get("returncode")
            returncode = value if isinstance(value, int) else None
            return True
        return False

    def drain(channel, line):
        if channel == "worker":
            handle_worker_line(line)
        else:
            handle_stderr(line)

    try:
        worker = Path(__file__).with_name("executor_worker.py")
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-B", str(worker)], cwd=project,
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
        tree = ProcessTree(process.pid)
        with db:
            db.execute("UPDATE tasks SET child_pid=? WHERE id=?", (process.pid, args.task))
        lines = queue.Queue()

        def pump(stream, channel):
            try:
                for line in iter(stream.readline, ""):
                    lines.put((channel, line))
            finally:
                lines.put((channel, None))

        readers = [threading.Thread(target=pump, args=(process.stdout, "worker"), daemon=True),
                   threading.Thread(target=pump, args=(process.stderr, "worker_stderr"), daemon=True)]
        for reader in readers:
            reader.start()

        request = json.dumps({"command": command, "prompt": prompt}, ensure_ascii=False)

        def feed():
            try:
                process.stdin.write(request)
                process.stdin.close()
            except (OSError, ValueError):
                pass

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        deadline = time.monotonic() + args.timeout
        finished = False
        eof = 0
        while not finished:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                channel, line = lines.get(timeout=min(0.2, remaining))
            except queue.Empty:
                continue
            if line is None:
                eof += 1
                if eof >= 2:
                    break
                continue
            if channel == "worker":
                finished = handle_worker_line(line)
            else:
                handle_stderr(line)
        if timed_out:
            tree.close()
            stop_process(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stop_process(process)
            process.wait()
        for reader in readers:
            reader.join(timeout=2)
        while True:
            try:
                channel, line = lines.get_nowait()
            except queue.Empty:
                break
            if line is not None:
                drain(channel, line)
        if returncode is None:
            returncode = process.returncode
        load_executor(args.config)  # Fail if route/config changed while executing.
    except (OSError, ValueError, KeyError, KeyboardInterrupt) as exc:
        control_error = f"执行中断：{exc}"
        result.update(ok=False, error=control_error, finished_at=now())
    finally:
        pending = delta_redactor.feed("", final=True)
        if pending:
            live.write("assistant_delta", {"text": pending})
            append_evidence({"type": "assistant_delta", "text": pending})
        pending = stderr_redactor.feed("", final=True)
        if pending:
            stderr_parts.append(pending)
            live.write("stderr", {"text": pending})
        if tree is not None:
            tree.close()
        if process is not None:
            stop_process(process)
            process.wait()
        if timed_out:
            result.update(ok=False, error="执行超时，已终止本次执行进程树；请复核已写入的文件")
        elif control_error or worker_error:
            result.update(ok=False, error=control_error or f"执行器读取中断：{worker_error}")
        elif final_payload is None:
            result.update(ok=False, error="执行器没有返回有效的 JSON 结果")
        elif parse_errors:
            result.update(ok=False, error="执行器输出包含无法解析的 JSON 行，已保留证据")
        else:
            ok = (returncode == 0 and isinstance(final_payload, dict)
                and final_payload.get("is_error") is False
                and final_payload.get("subtype") == "success"
                and not final_payload.get("permission_denials"))
            result.update(ok=ok, error=None if ok else "执行器失败、达到轮次限制或权限未满足")
        result.update(returncode=returncode, stdout="".join(stdout_parts),
                      stderr="".join(stderr_parts), session_id=session_id,
                      parse_errors=parse_errors, finished_at=result.get("finished_at") or now())
        live.write("finished", {"ok": result["ok"], "error": result.get("error"),
                                "returncode": returncode})
        live.close()
        safe = redact_text(json.dumps(result, ensure_ascii=False, indent=2), secrets)
        evidence.write_text(safe, encoding="utf-8")
        with db:
            db.execute("UPDATE tasks SET run_pid=NULL,child_pid=NULL,status=? WHERE id=?",
                       ("pending_review" if result["ok"] else "interrupted", args.task))
            event(db, args.task, "claude", "execution", safe)
        index(project, db)
    return {"ok": result["ok"], "evidence": str(evidence), "task": task_row(db, args.task),
            "error": result["error"], "dashboard_url": dashboard_url,
            "dashboard_error": result.get("dashboard_error")}


def parser():
    p = argparse.ArgumentParser(description="Codex / Claude 本地开发接力")
    p.add_argument("--project", required=True)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = p.add_subparsers(dest="action", required=True)
    commands.add_parser("init")
    commands.add_parser("status")
    new = commands.add_parser("new")
    new.add_argument("--title", required=True)
    new.add_argument("--plan-file", type=Path, required=True)
    for name in ("takeover", "checkpoint", "handoff"):
        command = commands.add_parser(name)
        command.add_argument("--task", required=True)
        if name != "handoff":
            command.add_argument("--agent", choices=("codex", "claude"), required=True)
        else:
            command.add_argument("--brief", action="store_true")
        if name == "checkpoint":
            command.add_argument("--file", type=Path, required=True)
    approve = commands.add_parser("approve")
    approve.add_argument("--task", required=True)
    approve.add_argument("--agent", choices=("codex", "claude"), required=True)
    approve.add_argument("--revision", required=True)
    approve.add_argument("--report", type=Path, required=True)
    review = commands.add_parser("review")
    review.add_argument("--task", required=True)
    review.add_argument("--agent", choices=("codex", "claude"), required=True)
    review.add_argument("--outcome", choices=("passed", "changes_requested"), required=True)
    review.add_argument("--file", type=Path, required=True)
    commands.add_parser("doctor")
    dashboard = commands.add_parser("dashboard")
    dashboard.add_argument("--port", type=int, default=0)
    dashboard.add_argument("--task")
    dashboard.add_argument("--no-open", action="store_true")
    dashboard.add_argument("--stop", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("--task", required=True)
    run.add_argument("--timeout", type=float, default=900)
    run.add_argument("--max-turns", type=int, default=20)
    return p


def execute(args):
    project = Path(args.project).resolve()
    if args.action == "doctor":
        return load_executor(args.config)[2]
    if args.action == "dashboard":
        from dashboard import dashboard_command
        return dashboard_command(args, project)
    if args.action == "run" and (args.timeout <= 0 or args.max_turns <= 0):
        raise ValueError("执行超时和最大轮次必须为正数")
    db = connect(project, initialize=args.action == "init")
    try:
        if args.action == "run":
            return run_task(args, project, db)
        with db:
            db.execute("BEGIN IMMEDIATE")
            if args.action == "new":
                task = uuid.uuid4().hex[:12]
                title = args.title.strip()
                if not title:
                    raise ValueError("任务标题不能为空")
                state = identity(project)
                db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (task, title, read_text(args.plan_file), "codex", "planned",
                            state["branch"], state["head"], None, None, None))
                event(db, task, "codex", "created", title)
                result = {"task": task_row(db, task)}
            elif args.action == "approve":
                ensure_idle(db)
                task = task_row(db, args.task, project)
                if args.agent != "codex" or task["owner"] != "codex":
                    raise ValueError("最终审核需要 Codex 接管并实际复核")
                if not task["start_sha"] or identity(project)["head"] != args.revision:
                    raise ValueError("审核提交不是当前 HEAD，或任务没有 Git 提交基线")
                for label, sha in (("任务起点", task["start_sha"]),
                                   ("审核起点", review_baseline(db, task))):
                    if sha and git(project, "merge-base", "--is-ancestor", sha, args.revision) is None:
                        raise ValueError(f"{label}不在当前 HEAD 历史中；历史可能被重写，请人工核对后再审核")
                if git(project, "status", "--porcelain", "--", ".", f":(exclude){RECORDS}/**") != "":
                    raise ValueError("代码工作区有未提交修改，请先固定审核提交")
                report = read_text(args.report)
                event(db, args.task, args.agent, "approved", report)
                db.execute("UPDATE tasks SET reviewed_sha=?,status='approved' WHERE id=?",
                           (args.revision, args.task))
                db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?)",
                           (task["branch"] or "(detached-or-no-git)", args.revision))
                result = {"task": task_row(db, args.task)}
            elif args.action == "review":
                ensure_idle(db)
                task = task_row(db, args.task, project)
                if args.agent != "codex" or task["owner"] != "codex":
                    raise ValueError("阶段审核需要 Codex 接管并实际复核")
                report = read_text(args.file)
                if args.outcome == "passed":
                    kind, status = "review_passed", "reviewed"
                else:
                    kind, status = "review_changes_requested", "changes_requested"
                event(db, args.task, args.agent, kind, report)
                db.execute("UPDATE tasks SET status=? WHERE id=?", (status, args.task))
                result = {"task": task_row(db, args.task)}
            elif args.action in ("takeover", "checkpoint"):
                task = task_row(db, args.task, project)
                if args.action == "takeover":
                    ensure_idle(db)
                    db.execute("UPDATE tasks SET owner=? WHERE id=?", (args.agent, args.task))
                    event(db, args.task, args.agent, "takeover", f'{task["owner"]} -> {args.agent}')
                else:
                    if args.agent != task["owner"]:
                        raise ValueError("请先接管任务，再保存检查点")
                    event(db, args.task, args.agent, "checkpoint", read_text(args.file))
                    db.execute("UPDATE tasks SET status=CASE WHEN run_pid IS NOT NULL THEN 'running' "
                               "ELSE 'pending_review' END WHERE id=?", (args.task,))
                result = {"task": task_row(db, args.task)}
            elif args.action == "handoff":
                result = (brief_handoff(db, project, args.task) if args.brief
                          else handoff(db, project, args.task))
            else:
                result = {"project": str(project), "tasks": summary(db), "git": identity(project)}
        index(project, db)
        return result
    finally:
        db.close()


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        result = execute(parser().parse_args())
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, sqlite3.Error, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    return 1 if result.get("ok") is False else 0


if __name__ == "__main__":
    sys.exit(main())
