"""Launch only after the controller has registered and protected this worker.

Executor stdout/stderr are forwarded line by line as JSON envelopes so the
controller can persist live events while the executor is still running.
"""
import json
import subprocess
import sys
import threading


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def pump(stream, kind):
    try:
        for line in iter(stream.readline, ""):
            emit({"kind": kind, "line": line})
    except (OSError, ValueError) as exc:
        emit({"kind": "worker_error", "error": f"{kind} 读取中断：{exc}"})
    finally:
        try:
            stream.close()
        except OSError:
            pass


def main():
    # EOF without a complete request means the controller died before releasing
    # the startup gate. In that case no external executor is ever launched.
    try:
        request = json.load(sys.stdin)
    except (ValueError, OSError):
        return 1
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    try:
        process = subprocess.Popen(request["command"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", bufsize=1)
    except (OSError, ValueError, KeyError) as exc:
        emit({"kind": "worker_error", "error": str(exc)})
        return 1

    threads = [threading.Thread(target=pump, args=(process.stdout, "stdout"), daemon=True),
               threading.Thread(target=pump, args=(process.stderr, "stderr"), daemon=True)]
    for thread in threads:
        thread.start()

    def feed():
        try:
            process.stdin.write(request["prompt"])
            process.stdin.close()
        except (OSError, ValueError):
            pass

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    returncode = process.wait()
    feeder.join(timeout=5)
    for thread in threads:
        thread.join(timeout=10)
    emit({"kind": "exit", "returncode": returncode})
    return returncode


if __name__ == "__main__":
    sys.exit(main())
