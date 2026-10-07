import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (BACKEND, os.path.join(BACKEND, "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

import fake_pg  # noqa: E402

fake_pg.install()  # 必须在 import db / api / worker 之前接管 psycopg
