"""测试路径与临时目录辅助。"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from privacy_publisher.model import parse_request, read_json

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


def fixture_request(name: str):
    return parse_request(read_json(FIXTURES / name))


def fresh_workdir() -> Path:
    """每个测试用例独立的临时工作目录，结束后由调用方清理。"""
    return Path(tempfile.mkdtemp(prefix="pp_test_"))


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
