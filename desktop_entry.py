"""Desktop launcher used by packaged builds."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

from app import main


def configure_log() -> Path:
    if sys.platform == "darwin":
        log_dir = Path.home() / "Library" / "Logs"
    elif os.environ.get("LOCALAPPDATA"):
        log_dir = Path(os.environ["LOCALAPPDATA"]) / "本地发票整理" / "Logs"
    else:
        log_dir = Path.home() / ".local" / "state" / "local-invoice-organizer"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "本地发票整理.log"
        log = log_path.open("a", encoding="utf-8", buffering=1)
    except OSError:
        log_path = Path(tempfile.gettempdir()) / "本地发票整理.log"
        log = log_path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log
    print(f"\n[{dt.datetime.now().isoformat(timespec='seconds')}] application starting")
    return log_path


def show_startup_error(log_path: Path) -> None:
    if sys.platform != "darwin":
        return
    message = f"本地发票整理启动失败。请查看日志：{log_path}"
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", f'tell application "System Events" to display alert "无法启动本地发票整理" message "{message}" as critical'],
            check=False,
            timeout=10,
        )
    except Exception:
        pass


if __name__ == "__main__":
    log_path = configure_log()
    if "--open-browser" not in sys.argv:
        sys.argv.append("--open-browser")
    try:
        main()
    except Exception:
        traceback.print_exc()
        show_startup_error(log_path)
        raise
