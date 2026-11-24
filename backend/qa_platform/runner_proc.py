"""一次一个 runner 子进程。密钥只走 stdin，Git 在子进程里做。"""

import os
import subprocess
import sys
from pathlib import Path

from qa_platform.executor import ExecutorSlot


def one_line(value: str) -> str:
    if any(char in value for char in "\r\n"):
        return ""
    return value


def local_secrets() -> dict[str, str]:
    path = Path(__file__).resolve().parent.parent / "secrets.env"
    if path.is_symlink() or not path.is_file():
        return {}
    found: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            found[key] = value.strip().strip('"').strip("'")
    return found


def launch(slot: ExecutorSlot, run_dir: Path, project_id: str, apply_now: bool = False) -> subprocess.Popen:
    env = os.environ.copy()
    file_secrets = local_secrets()
    deepseek = one_line(env.pop("DEEPSEEK_API_KEY", "") or file_secrets.get("DEEPSEEK_API_KEY", ""))
    yunxiao = one_line(env.pop("YUNXIAO_TOKEN", "") or file_secrets.get("YUNXIAO_TOKEN", ""))
    model = one_line(env.get("DEEPSEEK_MODEL", "") or file_secrets.get("DEEPSEEK_MODEL", ""))
    if model:
        env["DEEPSEEK_MODEL"] = model
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    log_path = run_dir / "runner.log"
    log = log_path.open("ab")
    command = [sys.executable, "-m", "qa_platform.runner.main", "--out", str(run_dir)]
    if apply_now:
        command.append("--apply-now")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    finally:
        log.close()
    slot.process = process
    slot.run_id = run_dir.name
    slot.project_id = project_id
    try:
        assert process.stdin is not None
        process.stdin.write(f"{deepseek}\n{yunxiao}\n".encode())
    finally:
        if process.stdin is not None:
            process.stdin.close()
    return process
