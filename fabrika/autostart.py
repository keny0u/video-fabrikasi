import os
from pathlib import Path

from . import config

SCRIPT_NAME = "VideoFabrikasi.vbs"


def _script_path():
    startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
    return startup / SCRIPT_NAME


def is_enabled():
    return _script_path().exists()


def enable():
    """Windows açılışında fabrikayı pencere göstermeden başlatır."""
    python = config.BASE / ".venv" / "Scripts" / "pythonw.exe"
    run_file = config.BASE / "run.py"
    command = f'""{python}"" ""{run_file}"" --arka'
    _script_path().write_text(
        f'CreateObject("WScript.Shell").Run "{command}", 0, False\n',
        encoding="utf-16",
    )


def disable():
    _script_path().unlink(missing_ok=True)
