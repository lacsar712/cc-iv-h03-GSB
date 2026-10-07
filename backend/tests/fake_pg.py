"""内存版 psycopg 替身：只实现本项目用到的接口，供 API/worker 回归测试使用。

语义关键点与真库对齐：
- execute 立即产生副作用（psycopg 语义），fetch 只取已缓存的结果；
- connect(row_factory=dict_row) 返回字典行；
- transaction() 进入时快照，异常退出时整体回滚（认领做到半路失败不留半空行）；
- INSERT ... RETURNING 回传完整新行，SELECT FOR UPDATE SKIP LOCKED 只认 pending。
"""
import copy
import re
import sys
import types


class FakePgError(RuntimeError):
    pass


class _State:
    def __init__(self):
        self.rows = []
        self.seq = 0
        self.fail_next_update = False

    def reset(self):
        self.rows = []
        self.seq = 0
        self.fail_next_update = False


STATE = _State()


class FakeTransaction:
    def __init__(self, conn):
        self.conn = conn
        self._snapshot = None

    def __enter__(self):
        self._snapshot = copy.deepcopy(STATE.rows)
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            # 真库事务回滚：本事务内一切修改撤销，行恢复 pending 且读数原样。
            STATE.rows = self._snapshot
        return False


class FakeCursor:
    def __init__(self, conn, sql, params):
        self.conn = conn
        self.sql = sql
        self.params = params or ()
        self._one = None
        self._all = None
        self._execute()

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all

    def _execute(self):
        s = " ".join(self.sql.split())

        if s.startswith("CREATE TABLE") or s.startswith("CREATE OR REPLACE FUNCTION") or \
           s.startswith("DROP TRIGGER") or s.startswith("CREATE TRIGGER"):
            return

        if "COUNT(*) AS n" in s:
            self._one = {"n": len(STATE.rows)}
            return

        m = re.match(r"INSERT INTO iv_scans \(([^)]+)\)\s*VALUES \(([^)]*)\)", s)
        if m:
            cols = [c.strip() for c in m.group(1).split(",")]
            tokens = [t.strip() for t in m.group(2).split(",")]
            params = list(self.params)
            values = []
            for tok in tokens:
                if tok == "%s":
                    values.append(params.pop(0))
                elif tok.startswith("'") and tok.endswith("'"):
                    values.append(tok[1:-1])
                else:
                    values.append(tok)
            row = dict(zip(cols, values))
            STATE.seq += 1
            row["id"] = STATE.seq
            row.setdefault("verdict", None)
            row.setdefault("reason", None)
            row.setdefault("processed_at", None)
            STATE.rows.append(row)
            returning = self._returning_cols(s)
            if returning:
                self._one = {k: copy.deepcopy(row[k]) for k in returning}
            return

        if "FOR UPDATE SKIP LOCKED" in s:
            pending = [r for r in STATE.rows if r["status"] == "pending"]
            if "WHERE id = %s" in s:
                wanted = self.params[0]
                found = next((r for r in pending if r["id"] == wanted), None)
            else:
                found = min(pending, key=lambda r: r["id"], default=None)
            if found is None:
                self._one = None
            else:
                self._one = {"id": found["id"], "fill_factor": found["fill_factor"]}
            return

        if s.startswith("UPDATE iv_scans SET status='done'"):
            verdict, reason, processed_at, scan_id = self.params
            # 模拟 UPDATE 落库途中连接中断：先改了内存状态再炸，事务 __exit__ 必须回滚。
            target = next(r for r in STATE.rows if r["id"] == scan_id)
            target.update(status="done", verdict=verdict, reason=reason,
                          processed_at=processed_at)
            if STATE.fail_next_update:
                STATE.fail_next_update = False
                raise FakePgError("simulated connection drop mid-UPDATE")
            return

        if s.startswith("SELECT id, string_code"):
            ordered = sorted(STATE.rows, key=lambda r: r["id"], reverse=True)
            cols = self._select_cols(s)
            self._all = [{k: copy.deepcopy(r.get(k)) for k in cols} for r in ordered]
            return

        raise AssertionError(f"fake pg cannot execute SQL: {s}")

    def _returning_cols(self, s):
        m = re.search(r"RETURNING (.+)$", s)
        if not m:
            return None
        return [c.strip() for c in m.group(1).split(",")]

    def _select_cols(self, s):
        m = re.match(r"SELECT (.+?) FROM iv_scans", s)
        return [c.strip() for c in m.group(1).split(",")]


class FakeConn:
    def __init__(self, *args, **kwargs):
        self._closed = False

    def execute(self, sql, params=None):
        return FakeCursor(self, sql, params)

    def commit(self):
        return None

    def rollback(self):
        return None

    def transaction(self):
        return FakeTransaction(self)

    def close(self):
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def connect(*args, **kwargs):
    return FakeConn(*args, **kwargs)


def install():
    """把伪造的 psycopg 模块树装进 sys.modules，必须在 import db/api/worker 之前调用。"""
    pg = types.ModuleType("psycopg")
    pg.connect = connect
    rows_mod = types.ModuleType("psycopg.rows")
    rows_mod.dict_row = lambda cursor: None  # 行工厂仅作标记，替身始终返回 dict
    pg.rows = rows_mod
    sys.modules["psycopg"] = pg
    sys.modules["psycopg.rows"] = rows_mod
