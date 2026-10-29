"""Launch only after the controller has registered and protected this worker."""
import json
import subprocess
import sys


def main():
    # EOF without a complete request means the controller died before releasing
    # the startup gate. In that case no external executor is ever launched.
    try:
        request = json.load(sys.stdin)
    except (ValueError, OSError):
        return 1
    result = subprocess.run(request["command"], input=request["prompt"], text=True,
                            encoding="utf-8", errors="replace")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
