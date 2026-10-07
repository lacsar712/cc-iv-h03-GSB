"""内存版 psycopg 连接替身。

本机没有 PostgreSQL / docker，又要端到端验证 HTTP 读回与写事务，
这里实现一个只覆盖本应用所用 SQL 形态的最小内存库，并保留真实的
事务隔离语义：未 commit 的写入对其它连接不可见，rollback 必须丢弃。
这样“写入半路失败不留半空行”才是可验证的，而不是走过场。
"""
from copy import deepcopy
from datetime import datetime, timezone

COLUMNS = [
    "id", "string_code", "voc_v", "isc_a", "fill_factor", "status",
    "verdict", "reason", "created_by", "created_at", "processed_at",
]

# 已提交的“表”与自增序列，所有连接共享。
_TABLE: list[dict] = []
_SEQ = {"id": 0}

# 测试钩子：提交时若新行的 string_code 命中则抛错，模拟半路失败。
FAIL_COMMIT_ON_CODE: str | None = None


def reset() -> None:
    _TABLE.clear()
    _SEQ["id"] = 0
    global FAIL_COMMIT_ON_CODE
    FAIL_COMMIT_ON_CODE = None


class _Cursor:
    def __init__(self, conn, sql, params):
        self.conn = conn
        self.sql = sql
        self.params = params or ()
        # 与真实 psycopg 一致：INSERT/UPDATE 的副作用在 execute 时立即发生，
        # 只有结果集（SELECT / RETURNING）等到 fetch 再取。
        self._kind, self._val = self._plan()

    def _plan(self):
        s = _compact(self.sql)
        if s.startswith("create") or s.startswith("drop"):
            return ("noop", None)
        if s.startswith("insert"):
            row = self.conn._stage_insert(self.sql, self.params)
            return ("row", row)
        if s.startswith("update"):
            return ("rowcount", self.conn._update(self.sql, self.params))
        if s.startswith("select"):
            return ("count" if s.startswith("select count") else "select", None)
        raise AssertionError(f"fake db 未覆盖的 SQL: {s[:60]}")

    def _selected(self):
        return self.conn._select(self.sql, self.params)

    def fetchone(self):
        if self._kind == "row":
            return deepcopy(self._val)
        if self._kind == "count":
            return {"n": len(_TABLE)}
        if self._kind == "select":
            rows = self._selected()
            return deepcopy(rows[0]) if rows else None
        return None

    def fetchall(self):
        if self._kind == "select":
            return deepcopy(self._selected())
        if self._kind == "row":
            return [deepcopy(self._val)]
        return []

    @property
    def rowcount(self):
        return self._val if self._kind == "rowcount" else -1


def _compact(sql: str) -> str:
    return " ".join(sql.split()).lower()


class _Transaction:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn._txn_savepoint = deepcopy(_TABLE), _SEQ["id"]
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            # 与 PostgreSQL 事务回滚一致：撤掉本事务内的已提交改动。
            table, seq = self.conn._txn_savepoint
            _TABLE.clear()
            _TABLE.extend(table)
            _SEQ["id"] = seq
        self.conn._txn_savepoint = None
        return False


class FakeConn:
    def __init__(self):
        # 本连接尚未提交的新行：对其它连接不可见。
        self._pending: list[dict] = []
        self._txn_savepoint = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return _Cursor(self, sql, params)

    def transaction(self):
        return _Transaction(self)

    def commit(self):
        global FAIL_COMMIT_ON_CODE
        offending = next(
            (r for r in self._pending if r["string_code"] == FAIL_COMMIT_ON_CODE), None
        )
        if offending is not None:
            # 模拟提交点失败：挂起的写入一律不落地。
            raise RuntimeError("simulated commit failure")
        _TABLE.extend(deepcopy(self._pending))
        self._pending.clear()

    def rollback(self):
        # 关键：半路失败时丢弃全部挂起写入，绝不留下半空行。
        self._pending.clear()

    # --- 供 _Cursor 调用的内部操作 ---
    def _stage_insert(self, sql, params):
        # 同时解析列清单与 VALUES 元组：%s 按序取参数，字面量（如
        # 'pending'/'done'）取字面值，二者与列名逐一对齐。
        cols, tokens = _parse_insert(sql)
        values = {}
        p = list(params or ())
        for col, tok in zip(cols, tokens):
            tok = tok.strip()
            if tok == "%s":
                values[col] = p.pop(0)
            else:
                values[col] = tok.strip("'")
        _SEQ["id"] += 1
        row = {
            "id": _SEQ["id"],
            "string_code": values.get("string_code"),
            "voc_v": values.get("voc_v"),
            "isc_a": values.get("isc_a"),
            "fill_factor": values.get("fill_factor"),
            "status": values.get("status", "pending"),
            "verdict": values.get("verdict"),
            "reason": values.get("reason"),
            "created_by": values.get("created_by"),
            "created_at": values.get("created_at", datetime.now(timezone.utc)),
            "processed_at": values.get("processed_at"),
        }
        # 入库行不允许关键字段缺失：真实表上这几列是 NOT NULL。
        for required in ("string_code", "voc_v", "isc_a", "fill_factor", "created_by", "created_at"):
            if row[required] is None:
                raise RuntimeError(f"NOT NULL 字段缺失: {required}")
        self._pending.append(row)
        return row

    def _select(self, sql, params):
        compact = _compact(sql)
        rows = list(_TABLE)
        if "where id = %s" in compact:
            rows = [r for r in rows if r["id"] == params[0]]
        if "status = 'pending'" in compact or "status='pending'" in compact:
            rows = [r for r in rows if r["status"] == "pending"]
        if "order by id desc" in compact:
            rows = sorted(rows, key=lambda r: r["id"], reverse=True)
        elif "order by id" in compact:
            rows = sorted(rows, key=lambda r: r["id"])
        if " limit 1" in compact:
            rows = rows[:1]
        return rows

    def _update(self, sql, params):
        compact = _compact(sql)
        if "set status='done'" in compact:
            verdict, reason, processed_at, scan_id = params
            for r in _TABLE:
                if r["id"] == scan_id:
                    r["status"] = "done"
                    r["verdict"] = verdict
                    r["reason"] = reason
                    r["processed_at"] = processed_at
                    return 1
        return 0


def _parse_insert(sql: str) -> tuple[list[str], list[str]]:
    # 列清单：第一个 ( ... )
    after_paren = sql.split("(", 1)[1]
    cols_blob, rest = after_paren.split(")", 1)
    cols = [c.strip() for c in cols_blob.split(",") if c.strip()]
    # VALUES 元组：VALUES 之后的第一个 ( ... )
    upper = rest.upper()
    vi = upper.index("VALUES") + len("VALUES")
    vals_blob = rest[vi:].split("(", 1)[1].split(")", 1)[0]
    tokens = [t.strip() for t in vals_blob.split(",")]
    return cols, tokens


def connect(*_args, **_kwargs):
    return FakeConn()
