"""Local project handoff. Python standard library only; no model API client."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid
from urllib.parse import urlparse
from process_tree import ProcessTree

RECORDS = "协作记录"
DEFAULT_CONFIG = Path.home() / ".project-relay" / "config.json"


def now():
    return datetime.now(timezone.utc).isoformat()


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


def handoff(db, project, task_id):
    task = task_row(db, task_id, project)
    events = [dict(r) for r in db.execute("SELECT * FROM events WHERE task=? ORDER BY sequence", (task_id,))]
    result = {"project": str(project), "task": task, "git": identity(project), "events": events,
              "checkpoints": [r for r in events if r["kind"] == "checkpoint"]}
    baseline = review_baseline(db, task)
    reviewed = db.execute("SELECT sha FROM reviews WHERE branch=?",
                          (task["branch"] or "(detached-or-no-git)",)).fetchone()
    if baseline and git(project, "merge-base", "--is-ancestor", baseline, result["git"]["head"] or "HEAD") is None:
        raise ValueError("审核起点不在当前历史中；请核对分支或历史重写，不能跳过待复核提交")
    result["review_from"] = baseline
    result["changes"] = git(project, "diff", "--stat", baseline, "--", ".", f":(exclude){RECORDS}/**") if baseline else None
    result["working_tree"] = git(project, "status", "--short", "--", ".", f":(exclude){RECORDS}/**")
    result["last_reviewed_sha"] = reviewed["sha"] if reviewed else None
    result["needs_review"] = not reviewed or result["git"]["head"] != reviewed["sha"] or bool(result["working_tree"])
    return result


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


def process_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied: treat as running.
        try:
            status = wintypes.DWORD()
            return not api.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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
        config, env, metadata = load_executor(args.config)
    except (ValueError, OSError, KeyError) as exc:
        record_rejection(project, db, args.task, exc)
        raise
    with db:
        db.execute("BEGIN IMMEDIATE")
        ensure_idle(db)
        task = task_row(db, args.task, project)
        event(db, args.task, "claude", "dispatched", json.dumps(metadata, ensure_ascii=False))
        db.execute("UPDATE tasks SET owner='claude',status='running',run_pid=?,child_pid=NULL WHERE id=?",
                   (os.getpid(), args.task))
    folder = project / RECORDS / args.task
    folder.mkdir(exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    evidence = folder / f"执行-{run_id}.json"
    prompt = ("你是本机项目执行负责人，实际模型由 CC Switch 路由决定。使用简体中文。\n"
              "执行下面计划，遵守用户确认的范围、项目规范与验收条件。完成后报告修改、实际测试、未验证事项、下一步。\n"
              "按阶段保存进度；你的完成结果进入待 Codex 复核。只执行本任务，不自动提交、推送、部署。\n"
              + json.dumps(handoff(db, project, args.task), ensure_ascii=False))
    command = config["command"] + ["--print", "--output-format", "json", "--model", config["model"],
        "--permission-mode", "acceptEdits", "--allowedTools", "Read,Edit,Write,Bash,Glob,Grep",
        "--max-turns", str(args.max_turns)]
    result = {"started_at": now(), "route": metadata, "ok": False, "error": "执行尚未结束"}
    evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    process = None
    tree = None
    try:
        worker = Path(__file__).with_name("executor_worker.py")
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-B", str(worker)], cwd=project, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        tree = ProcessTree(process.pid)
        with db:
            db.execute("UPDATE tasks SET child_pid=? WHERE id=?", (process.pid, args.task))
        try:
            output, errors = process.communicate(json.dumps({"command": command, "prompt": prompt}, ensure_ascii=False),
                                                 timeout=args.timeout)
        except subprocess.TimeoutExpired:
            tree.close()
            stop_process(process)
            output, errors = process.communicate()
            result["error"] = "执行超时，已终止本次执行进程树；请复核已写入的文件"
        else:
            try:
                payload = json.loads(output)
                result["ok"] = (process.returncode == 0 and isinstance(payload, dict)
                    and payload.get("is_error") is False and payload.get("subtype") == "success"
                    and not payload.get("permission_denials"))
                result["error"] = None if result["ok"] else "执行器失败、达到轮次限制或权限未满足"
            except json.JSONDecodeError:
                result["error"] = "执行器没有返回有效的 JSON 结果"
        result.update(returncode=process.returncode, stdout=output, stderr=errors, finished_at=now())
        load_executor(args.config)  # Fail if route/config changed while executing.
    except (OSError, ValueError, KeyError, KeyboardInterrupt) as exc:
        result.update(ok=False, error=f"执行中断：{exc}", finished_at=now())
    finally:
        if tree is not None:
            tree.close()
        if process is not None:
            stop_process(process)
            process.wait()
        safe = json.dumps(result, ensure_ascii=False, indent=2).replace(env["ANTHROPIC_AUTH_TOKEN"], "[REDACTED]")
        evidence.write_text(safe, encoding="utf-8")
        with db:
            db.execute("UPDATE tasks SET run_pid=NULL,child_pid=NULL,status=? WHERE id=?",
                       ("pending_review" if result["ok"] else "interrupted", args.task))
            event(db, args.task, "claude", "execution", safe)
        index(project, db)
    return {"ok": result["ok"], "evidence": str(evidence), "task": task_row(db, args.task),
            "error": result["error"]}


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
        if name == "checkpoint":
            command.add_argument("--file", type=Path, required=True)
    approve = commands.add_parser("approve")
    approve.add_argument("--task", required=True)
    approve.add_argument("--agent", choices=("codex", "claude"), required=True)
    approve.add_argument("--revision", required=True)
    approve.add_argument("--report", type=Path, required=True)
    commands.add_parser("doctor")
    run = commands.add_parser("run")
    run.add_argument("--task", required=True)
    run.add_argument("--timeout", type=float, default=900)
    run.add_argument("--max-turns", type=int, default=20)
    return p


def execute(args):
    project = Path(args.project).resolve()
    if args.action == "doctor":
        return load_executor(args.config)[2]
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
                result = handoff(db, project, args.task)
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
