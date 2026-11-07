"""Read-only local dashboard for project relay.

The HTTP process is deliberately independent from the relay worker job: it
reads the project's SQLite database in read-only mode and the per-attempt live
JSONL files, exposes no write endpoint, and is stopped only by the local CLI.
"""
import argparse
from datetime import datetime, timezone
import http.client
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from process_tree import process_alive

RECORDS = "协作记录"
SERVER_DIR = "看板"
INFO_NAME = "server.json"
TASK_ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_EVENT_BODY = 20000
MAX_ATTEMPT_EVENTS = 5000
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
STATUS_LABELS = {
    "planned": "已规划",
    "running": "执行中",
    "pending_review": "待复核",
    "interrupted": "已中断",
    "reviewed": "阶段复核通过，待最终验收",
    "changes_requested": "需返工",
    "approved": "最终验收通过",
}
REVIEW_KINDS = ("review_passed", "review_changes_requested", "approved")


class StateError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat()


def dashboard_folder(project):
    return Path(project) / RECORDS / SERVER_DIR


def info_path(project):
    return dashboard_folder(project) / INFO_NAME


def _db_path(project):
    return Path(project) / RECORDS / "state.sqlite3"


def _connect_ro(project):
    path = _db_path(project)
    if not path.is_file():
        raise StateError(409, "项目尚未初始化，请先运行 init")
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    except sqlite3.Error as exc:
        raise StateError(500, f"无法只读打开项目状态：{exc}") from exc
    db.row_factory = sqlite3.Row
    return db


def _validate_task(project, task):
    if not task or not TASK_ID_RE.match(task):
        raise ValueError("任务 ID 格式无效")
    try:
        db = _connect_ro(project)
    except StateError as exc:
        raise ValueError(str(exc)) from exc
    try:
        row = db.execute("SELECT id FROM tasks WHERE id=?", (task,)).fetchone()
    finally:
        db.close()
    if row is None:
        raise ValueError("任务不存在")


def _read_jsonl(path):
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise StateError(500, f"无法读取实时日志：{exc}") from exc
    text = data.decode("utf-8", errors="replace")
    incomplete = bool(text) and not text.endswith("\n")
    lines = text.splitlines()
    if incomplete and lines:
        lines = lines[:-1]
    events = []
    parse_errors = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if isinstance(item, dict):
            events.append(item)
        else:
            parse_errors += 1
    return events, incomplete, parse_errors


def _ignored_event(item):
    if item.get("kind") != "system":
        return False
    payload = item.get("payload")
    return isinstance(payload, dict) and payload.get("subtype") == "thinking_tokens"


def _read_attempts(project, task):
    folder = Path(project) / RECORDS / task
    attempts = []
    if not folder.is_dir():
        return attempts
    files = [path for path in folder.glob("live-*.jsonl") if path.is_file()]
    files.sort(key=lambda path: (path.stat().st_mtime_ns, path.name))
    for number, path in enumerate(files, 1):
        raw_events, incomplete, parse_errors = _read_jsonl(path)
        events = [item for item in raw_events if not _ignored_event(item)]
        ignored = len(raw_events) - len(events)
        started_at = events[0].get("at") if events else None
        total = len(events)
        offset = max(0, total - MAX_ATTEMPT_EVENTS)
        events = events[offset:]
        truncated = offset > 0
        finished = next((item for item in reversed(events) if item.get("kind") == "finished"), None)
        payload = finished.get("payload") if isinstance(finished, dict) else None
        attempts.append({
            "id": path.stem[len("live-"):],
            "number": number,
            "file": path.name,
            "started_at": started_at,
            "ended": finished is not None,
            "end_ok": payload.get("ok") if isinstance(payload, dict) else None,
            "end_error": payload.get("error") if isinstance(payload, dict) else None,
            "incomplete_tail": incomplete,
            "parse_errors": parse_errors,
            "ignored_events": ignored,
            "truncated": truncated,
            "event_offset": offset,
            "event_total": total,
            "events": events,
        })
    return attempts


def _public_event(row):
    body = row["body"] or ""
    return {
        "sequence": row["sequence"],
        "at": row["at"],
        "agent": row["agent"],
        "kind": row["kind"],
        "body": body[:MAX_EVENT_BODY],
        "body_truncated": len(body) > MAX_EVENT_BODY,
    }


def _max_time(values):
    values = [value for value in values if isinstance(value, str) and value]
    return max(values) if values else None


def _derive_status(task, attempts, controller_alive):
    status = task["status"]
    attempt_count = len(attempts)
    rework = status == "changes_requested" or (
        attempt_count >= 2 and status in ("running", "pending_review", "reviewed"))
    label = STATUS_LABELS.get(status, status)
    detail = "等待派活"
    stage = status if status in STATUS_LABELS else "planned"
    if status == "running" and not controller_alive:
        stage = "interrupted"
        label = "执行进程已退出"
        detail = "控制进程已退出；已保留最近事件与检查点，状态等待更新"
    elif status == "running":
        label = "返工执行中" if rework else "执行中"
        detail = "执行进程运行中"
    elif status == "pending_review":
        label = "返工待复核" if rework else "待复核"
        detail = "执行结束，等待 Codex 复核"
    elif status == "reviewed":
        detail = "Codex 阶段复核通过；等待最终验收"
    elif status == "changes_requested":
        detail = "Codex 要求返工；修复后再次派活"
    elif status == "interrupted":
        detail = "上次执行失败或中断，请查看日志"
    elif status == "approved":
        revision = task.get("reviewed_sha") or ""
        detail = f"最终验收基线 {revision[:12]}" if revision else "最终验收通过"
    return {
        "stage": stage,
        "status_label": label,
        "status_detail": detail,
        "rework": rework,
        "attempt_count": attempt_count,
        "controller_alive": controller_alive,
        "execution_alive": controller_alive,
    }


def build_state(project, task=None, server_info=None):
    project = Path(project)
    db = _connect_ro(project)
    try:
        tasks = [dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY rowid")]
        payload = {
            "project": project.name,
            "project_root": str(project),
            "server": server_info or {},
            "tasks": [{"id": row["id"], "title": row["title"], "owner": row["owner"],
                       "status": row["status"]} for row in tasks],
            "now": now(),
        }
        if task is None:
            return payload
        if not TASK_ID_RE.match(task):
            raise StateError(400, "任务 ID 格式无效")
        row = next((item for item in tasks if item["id"] == task), None)
        if row is None:
            raise StateError(404, "任务不存在")
        events = [dict(item) for item in db.execute(
            "SELECT * FROM events WHERE task=? ORDER BY sequence", (task,))]
    finally:
        db.close()
    attempts = _read_attempts(project, task)
    live = attempts[-1]["events"] if attempts else []
    controller_alive = process_alive(row["run_pid"]) or process_alive(row["child_pid"])
    derived = _derive_status(row, attempts, controller_alive)
    public_events = [_public_event(event) for event in events]
    reviews = [event for event in public_events if event["kind"] in REVIEW_KINDS]
    checkpoints = [event for event in public_events if event["kind"] == "checkpoint"]
    last_event_at = _max_time([event["at"] for event in events] +
                              [event.get("at") for event in live])
    payload.update(derived)
    payload.update({
        "task": row,
        "plan": row["plan"],
        "events": public_events,
        "reviews": [{"sequence": item["sequence"], "kind": item["kind"], "at": item["at"],
                     "agent": item["agent"], "body": item["body"],
                     "body_truncated": item["body_truncated"]}
                    for item in reviews],
        "checkpoints": checkpoints,
        "attempts": attempts,
        "live": live,
        "last_event_at": last_event_at,
    })
    return payload


def _clean_env():
    env = os.environ.copy()
    for name in list(env):
        if name.startswith("ANTHROPIC_") or name in (
                "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE"):
            env.pop(name, None)
    return env


def _health(info, timeout=1.5):
    try:
        port = int(info["port"])
    except (KeyError, TypeError, ValueError):
        return False
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/api/health?token=" + str(info.get("token", "")))
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()
    return (response.status == 200 and payload.get("ok") is True
            and payload.get("instance") == info.get("instance")
            and payload.get("pid") == info.get("pid"))


def _load_info(project):
    path = info_path(project)
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    if info.get("project") != str(Path(project).resolve()):
        return None
    if not isinstance(info.get("pid"), int) or not process_alive(info["pid"]):
        return None
    return info


def _build_url(info, task):
    params = {"token": info["token"]}
    if task:
        params["task"] = task
    return f"http://127.0.0.1:{info['port']}/?{urlencode(params)}"


def _open_browser(url):
    try:
        import webbrowser
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _result(project, info, task, reused, log=None, opened=None):
    result = {
        "url": _build_url(info, task),
        "pid": info["pid"],
        "port": info["port"],
        "token": info["token"],
        "instance": info["instance"],
        "reused": reused,
        "task": task,
        "info": str(info_path(project)),
    }
    if log is not None:
        result["log"] = str(log)
    if opened is not None:
        result["opened"] = opened
    return result


def _terminate(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def _log_tail(path, limit=2000):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")[-limit:]


def start(project, task=None, port=0, open_browser=True):
    project = Path(project).resolve()
    if not project.is_dir():
        raise ValueError("项目目录不存在")
    if port < 0 or port > 65535:
        raise ValueError("端口必须在 0 到 65535 之间")
    try:
        if task:
            _validate_task(project, task)
        else:
            _connect_ro(project).close()
    except StateError as exc:
        raise ValueError(str(exc)) from exc
    except OSError as exc:
        raise ValueError(f"项目尚未初始化，请先运行 init：{exc}") from exc
    folder = dashboard_folder(project)
    folder.mkdir(parents=True, exist_ok=True)
    existing = _load_info(project)
    if existing is not None and _health(existing):
        opened = _open_browser(_build_url(existing, task)) if open_browser else None
        return _result(project, existing, task, True, opened=opened)
    if existing is not None:
        try:
            info_path(project).unlink()
        except OSError:
            pass

    token = secrets.token_hex(16)
    instance = uuid.uuid4().hex
    log_path = folder / f"server-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{instance[:8]}.log"
    command = [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
               "--project", str(project), "--port", str(port), "--token", token,
               "--instance", instance, "--info", str(info_path(project))]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    log = open(log_path, "ab", buffering=0)
    try:
        child = subprocess.Popen(command, cwd=str(Path(__file__).resolve().parent),
            env=_clean_env(), stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, creationflags=creationflags, close_fds=True)
    except OSError as exc:
        log.close()
        raise ValueError(f"看板服务启动失败：无法启动后台进程：{exc}") from exc
    finally:
        try:
            log.close()
        except OSError:
            pass
    deadline = time.monotonic() + 8
    info = None
    while time.monotonic() < deadline:
        if child.poll() is not None:
            break
        try:
            candidate = json.loads(info_path(project).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            candidate = None
        if isinstance(candidate, dict) and candidate.get("instance") == instance:
            if _health(candidate):
                info = candidate
                break
        time.sleep(0.05)
    if info is None:
        if child.poll() is None:
            _terminate(child.pid)
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        try:
            stale = json.loads(info_path(project).read_text(encoding="utf-8"))
            if stale.get("instance") == instance:
                info_path(project).unlink()
        except (OSError, ValueError):
            pass
        raise ValueError(
            "看板服务启动失败：端口可能被占用或服务无法绑定 127.0.0.1；"
            f"日志：{log_path}\n{_log_tail(log_path)}")
    opened = _open_browser(_build_url(info, task)) if open_browser else None
    return _result(project, info, task, False, log=log_path, opened=opened)


def stop(project):
    project = Path(project).resolve()
    path = info_path(project)
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"stopped": False, "message": "没有运行中的看板服务"}
    if not isinstance(info, dict) or info.get("project") != str(project):
        try:
            path.unlink()
        except OSError:
            pass
        return {"stopped": False, "message": "看板记录不属于当前项目，未终止任何进程"}
    pid = info.get("pid")
    healthy = isinstance(pid, int) and process_alive(pid) and _health(info)
    if healthy:
        _terminate(pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process_alive(pid):
            time.sleep(0.05)
    try:
        path.unlink()
    except OSError:
        pass
    if healthy:
        return {"stopped": True, "pid": pid, "port": info.get("port")}
    return {"stopped": False, "message": "看板服务未在运行，已清理过期记录"}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    allow_reuse_port = False

    def __init__(self, address, project, token, instance, assets, info_file):
        self.project = project
        self.token = token
        self.instance = instance
        self.assets = assets
        self.info_file = info_file
        self.started_at = now()
        super().__init__(address, DashboardHandler)

    def public_info(self):
        return {
            "pid": os.getpid(),
            "port": self.server_address[1],
            "started_at": self.started_at,
            "instance": self.instance,
        }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "RelayDashboard/1.0"

    def log_message(self, format, *args):
        path = self.path.split("?", 1)[0]
        sys.stderr.write(f"{self.command} {path}\n")
        sys.stderr.flush()

    def _check_local(self):
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in allowed:
            self._json(403, {"error": "仅允许本机看板地址访问"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed}:
            self._json(403, {"error": "拒绝跨站请求"})
            return False
        return True

    def _token_ok(self):
        query = parse_qs(urlsplit(self.path).query)
        provided = query.get("token", [None])[0] or self.headers.get("X-Dashboard-Token")
        return bool(provided) and hmac.compare_digest(str(provided), self.server.token)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        name, content_type = STATIC_FILES[path]
        try:
            body = (self.server.assets / name).read_bytes()
        except OSError:
            self._json(500, {"error": "静态资源缺失"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                         "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._check_local():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/health":
                if not self._token_ok():
                    self._json(401, {"error": "缺少或无效的看板 token"})
                    return
                self._json(200, {"ok": True, **self.server.public_info()})
                return
            if path == "/api/state":
                if not self._token_ok():
                    self._json(401, {"error": "缺少或无效的看板 token"})
                    return
                task = parse_qs(urlsplit(self.path).query).get("task", [None])[0]
                self._json(200, build_state(self.server.project, task,
                                            self.server.public_info()))
                return
            if path in STATIC_FILES:
                self._static(path)
                return
            self._json(404, {"error": "资源不存在"})
        except StateError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (OSError, sqlite3.Error) as exc:
            sys.stderr.write(f"状态读取失败：{exc}\n")
            sys.stderr.flush()
            self._json(500, {"error": "读取看板状态失败，请检查项目协作记录"})
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self._json(500, {"error": f"看板内部错误：{exc}"})

    def _read_only(self):
        if not self._check_local():
            return
        self._json(405, {"error": "看板为只读接口，不支持该请求方法"})

    do_POST = _read_only
    do_PUT = _read_only
    do_PATCH = _read_only
    do_DELETE = _read_only
    do_OPTIONS = _read_only


def serve(project, port, token, instance, info_file):
    project = Path(project).resolve()
    if not _db_path(project).is_file():
        raise SystemExit("项目尚未初始化，请先运行 init")
    assets = Path(__file__).resolve().parent.parent / "dashboard"
    info_file = Path(info_file)
    server = DashboardServer(("127.0.0.1", port), project, token, instance, assets, info_file)
    info = {"project": str(project), **server.public_info(), "token": token}
    temp = info_file.with_name(f".{info_file.name}.{instance}.tmp")
    temp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(info_file)
    print(f"看板服务已启动：http://127.0.0.1:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        try:
            current = json.loads(info_file.read_text(encoding="utf-8"))
            if current.get("instance") == instance:
                info_file.unlink()
        except (OSError, ValueError):
            pass


def dashboard_command(args, project):
    if args.stop:
        return stop(project)
    return start(project, task=args.task, port=args.port,
                 open_browser=not args.no_open)


def main():
    parser = argparse.ArgumentParser(description="项目接力只读看板服务")
    parser.add_argument("--project", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--info", required=True)
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    serve(Path(args.project), args.port, args.token, args.instance, Path(args.info))


if __name__ == "__main__":
    main()
