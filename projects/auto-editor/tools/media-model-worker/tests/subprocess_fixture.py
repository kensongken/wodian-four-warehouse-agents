"""Single-process overflow fixture, independent of Python cold-start time.

Use shell builtins only: no grandchild can retain the pipe or evade the reap check.
Production timeouts/caps and existing test assertions are unchanged.
"""
from pathlib import Path

def shell_overflow_executable(root: Path, name: str, stream: str) -> Path:
    if stream not in {"stdout", "stderr"}:
        raise ValueError("unsupported fixture stream")
    redirect = " >&2" if stream == "stderr" else ""
    body = "#!/bin/sh\ni=0\nwhile [ \"$i\" -lt 70 ]; do\n  printf '%s' '" + "x" * 1024 + "'" + redirect + "\n  i=$((i + 1))\ndone\nwhile :; do :; done\n"
    path = root / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)
    return path.resolve()
