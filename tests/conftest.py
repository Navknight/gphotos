import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

FIXTURES = Path(__file__).parent / "fixtures"
TAKEOUT = FIXTURES / "mini_takeout" / "Takeout"

needs_exiftool = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed")


@pytest.fixture(scope="session")
def takeout():
    """The mini Takeout, built on demand so binaries stay out of the repo."""
    if not TAKEOUT.exists():
        subprocess.run([sys.executable, str(FIXTURES / "build.py")], check=True)
    return TAKEOUT


@pytest.fixture
def cfg(tmp_path):
    from gphotos import config
    c = config.Config(data=tmp_path / "data", output=tmp_path / "out")
    c.mkdirs()
    return c
