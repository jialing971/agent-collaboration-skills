"""本地项目交接记录；仅使用 Python 标准库，不请求网络。"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def project_context(project):
    root = Path(project).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("项目路径必须是目录")
    identity = root
    context = {"project_root": str(root), "branch": "(无 Git)", "head": None, "dirty": None}
    git = shutil.which("git")
    if git:
        def run_git(*args):
            return subprocess.run([git, "--no-optional-locks", "-C", str(root), *args],
                                  capture_output=True, text=True, encoding="utf-8", timeout=20)

        probe = run_git("rev-parse", "--show-toplevel")
        if probe.returncode == 0:
            context["project_root"] = probe.stdout.strip()
            common = run_git("rev-parse", "--git-common-dir")
            branch = run_git("symbolic-ref", "--quiet", "--short", "HEAD")
            head = run_git("rev-parse", "--verify", "HEAD")
            status = run_git("status", "--porcelain")
            if common.returncode or status.returncode:
                raise ValueError("Git 状态读取失败，暂停保存，避免记录到错误项目")
            identity = (root / common.stdout.strip()).resolve()
            context["head"] = head.stdout.strip() if head.returncode == 0 else None
            if branch.returncode == 0:
                context["branch"] = branch.stdout.strip()
            elif context["head"]:
                context["branch"] = "detached:" + context["head"]
            else:
                raise ValueError("无法确定 Git 分支或提交")
            context["dirty"] = bool(status.stdout)
        elif "not a git repository" not in probe.stderr.lower():
            raise ValueError("Git 项目识别失败：" + probe.stderr.strip())
    elif any((p / ".git").exists() for p in [root, *root.parents]):
        raise ValueError("项目存在 .git，但 Git 不可用；请先恢复 Git 路径")
    context["project_key"] = hashlib.sha256(
        os.path.normcase(str(identity)).encode("utf-8")).hexdigest()
    return context


def save_record(store, context, args):
    body = Path(args.file).read_text(encoding="utf-8-sig").strip()
    if not body or not args.title.strip():
        raise ValueError("交接内容和标题均不能为空")
    store.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(store, timeout=20) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_key TEXT NOT NULL, branch TEXT NOT NULL,
            agent TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
            created_at TEXT NOT NULL, context TEXT NOT NULL)""")
        row = db.execute("""INSERT INTO checkpoints
            (project_key, branch, agent, title, body, created_at, context)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (context["project_key"], context["branch"], args.agent, args.title.strip(),
             body, datetime.now(timezone.utc).isoformat(),
             json.dumps(context, ensure_ascii=False)))
        record_id = row.lastrowid
    return {"saved": True, "id": record_id, "store": str(store), "context": context}


def recall_records(store, context, args):
    if not store.exists():
        return {"context": context, "records": []}
    conditions = ["project_key=?", "branch=?"]
    parameters = [context["project_key"], context["branch"]]
    if args.command == "search":
        words = args.query.split()
        if not words:
            raise ValueError("请输入关键词；多个词用空格分开，按同时包含匹配")
        for word in words:
            conditions.append("instr(lower(title || char(10) || body), lower(?)) > 0")
            parameters.append(word)
    parameters.append(args.limit)
    with sqlite3.connect(store.as_uri() + "?mode=ro", uri=True, timeout=20) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM checkpoints WHERE " + " AND ".join(conditions)
                          + " ORDER BY id DESC LIMIT ?", parameters).fetchall()
    records = []
    for row in rows:
        record = dict(row)
        record["context"] = json.loads(record["context"])
        records.append(record)
    return {"context": context, "records": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.getcwd(), help="项目目录")
    parser.add_argument("--store", default=str(Path.home() / ".shared-project-memory/data/memory.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("context", help="只读核对项目身份、分支和 Git 状态")
    save = commands.add_parser("save", help="追加保存交接 Markdown")
    save.add_argument("--agent", required=True, choices=["codex", "claude"])
    save.add_argument("--title", required=True)
    save.add_argument("--file", required=True, help="UTF-8 Markdown 文件")
    recall = commands.add_parser("recall", help="读取当前项目的交接记录")
    recall.add_argument("--limit", type=int, default=5)
    search = commands.add_parser("search", help="关键词检索，空格分隔的词需同时命中")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    try:
        if args.command in ("recall", "search") and not 1 <= args.limit <= 50:
            raise ValueError("limit 必须在 1 到 50 之间")
        if args.command == "search" and not args.query.strip():
            raise ValueError("检索关键词不能为空")
        context = project_context(args.project)
        store = Path(args.store).resolve()
        if args.command == "context":
            result = context
        elif args.command == "save":
            result = save_record(store, context, args)
        else:
            result = recall_records(store, context, args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
