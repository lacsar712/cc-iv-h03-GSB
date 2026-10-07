"""测试装配：在导入应用之前把 db.connect 指向内存假库。

api 在导入时执行 `from db import connect` 并立即 seed()，所以替换必须
发生在 `import api` 之前。conftest 由 pytest 最先导入，时机正好。
"""
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "postgresql://app:app@localhost:54402/pvivscan")

from tests import fakedb  # noqa: E402
import db  # noqa: E402

db.connect = fakedb.connect

import api  # noqa: E402  (导入即 seed，使用上面替换后的 connect)


@pytest.fixture(autouse=True)
def _fresh_table():
    # 每个用例清空后重新播种，保证互不串数据。
    fakedb.reset()
    api.seed()
    yield
    fakedb.reset()
