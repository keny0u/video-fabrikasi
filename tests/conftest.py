import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["FABRIKA_HOME"] = tempfile.mkdtemp(prefix="fabrika-test-")
os.environ["FABRIKA_DENEME"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from fabrika import config, db


@pytest.fixture(autouse=True)
def fresh_db():
    config.DB_PATH.unlink(missing_ok=True)
    shutil.rmtree(config.CLIPS_DIR, ignore_errors=True)
    db.init()


@pytest.fixture(autouse=True)
def no_clone_voice(monkeypatch):
    """Testler gerçek Chatterbox'ı (ayrı ortam, ekran kartı) çalıştırmasın; gereken test kendisi açar."""
    from fabrika.story import voice

    monkeypatch.setattr(voice, "CLONE_PYTHON", Path(os.environ["FABRIKA_HOME"]) / "yok" / "python.exe")
