"""H03 回归：入库的填充因子在总表与详情卡两面都不得漂没；观察员不得写；
投影做到半路失败时事务回滚，不留半空行；反复刷新读数稳定。"""
import os

import pytest
from litestar.testing import TestClient

import api
import worker
from fake_pg import STATE, FakePgError


@pytest.fixture(autouse=True)
def fresh_seed():
    STATE.reset()
    api.seed()
    yield
    STATE.reset()


@pytest.fixture()
def client():
    with TestClient(app=api.app) as c:
        yield c


def token(client, username, password):
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.is_success
    return res.json()["access_token"]


def auth(token_value):
    return {"Authorization": f"Bearer {token_value}"}


def by_code(rows):
    return {r["string_code"]: r for r in rows}


SEED_FF = {"阵列A-串03": (0.78, "合格"), "阵列B-串11": (0.61, "衰减")}


def test_seed_readings_survive_on_total_table(client):
    """种子样例读数（0.78 / 0.61）在总表上不得变成空白或零。"""
    h = auth(token(client, "scanner", "scan123456"))
    rows = client.get("/api/logs", headers=h).json()
    table = by_code(rows)
    assert set(table) == set(SEED_FF)
    for code, (ff, verdict) in SEED_FF.items():
        row = table[code]
        assert row["fill_factor"] == ff
        assert row["fill_factor"] not in (None, 0, "")
        assert row["verdict"] == verdict


def test_create_surface_and_list_surface_share_real_ff(client):
    """提交回包（详情面）与总表回包都带真实填充因子，处理前后都不被抹掉。"""
    h = auth(token(client, "scanner", "scan123456"))
    created = client.post("/api/logs", headers=h, json={
        "string_code": "阵列C-串05", "voc_v": 40.1, "isc_a": 8.8, "fill_factor": 0.83,
    })
    assert created.status_code == 201
    body = created.json()
    assert body["fill_factor"] == 0.83          # create/详情面
    assert body["status"] == "pending"

    rows = client.get("/api/logs", headers=h).json()
    assert by_code(rows)["阵列C-串05"]["fill_factor"] == 0.83  # list/总表面

    with api.connect() as work:                  # 工人处理出结论
        assert worker.claim_id(work, body["id"])
        work.commit()

    rows = client.get("/api/logs", headers=h).json()
    done = by_code(rows)["阵列C-串05"]
    assert done["fill_factor"] == 0.83           # 处理后读数仍在
    assert done["status"] == "done"
    assert done["verdict"] == "合格"


def test_repeated_refresh_never_blanks_or_zeroes_ff(client):
    """反复刷新/翻页回来：同一条记录的填充因子逐次一致，绝不凭空变空白或零。"""
    h = auth(token(client, "scanner", "scan123456"))
    snapshots = []
    for _ in range(5):
        rows = client.get("/api/logs", headers=h).json()
        snapshots.append({r["id"]: r["fill_factor"] for r in rows})
    first = snapshots[0]
    assert first  # 非空
    for snap in snapshots[1:]:
        assert snap == first
        for value in snap.values():
            assert value not in (None, 0, "")


def test_watcher_is_read_only(client):
    """观察员只读：提交 403 且不落库；读取照常。"""
    h = auth(token(client, "watcher", "watch123456"))
    assert client.get("/api/logs", headers=h).status_code == 200

    before = client.get("/api/logs", headers=h).json()
    res = client.post("/api/logs", headers=h, json={
        "string_code": "阵列X-串99", "voc_v": 39.0, "isc_a": 8.0, "fill_factor": 0.75,
    })
    assert res.status_code == 403
    after = client.get("/api/logs", headers=h).json()
    assert len(after) == len(before)
    assert all(r["string_code"] != "阵列X-串99" for r in after)


def test_anonymous_cannot_read_or_write(client):
    assert client.get("/api/logs").status_code == 401
    res = client.post("/api/logs", json={
        "string_code": "阵列X-串98", "voc_v": 39.0, "isc_a": 8.0, "fill_factor": 0.75,
    })
    assert res.status_code == 401


def test_worker_midway_failure_leaves_no_half_row(client):
    """认领 UPDATE 落库途中失败：事务整体回滚，行仍是完整 pending 行（FF 原样），
    之后可以重新认领成功——任何时刻都不存在半空行。"""
    h = auth(token(client, "scanner", "scan123456"))
    created = client.post("/api/logs", headers=h, json={
        "string_code": "阵列D-串07", "voc_v": 42.0, "isc_a": 9.0, "fill_factor": 0.66,
    }).json()
    scan_id = created["id"]

    stored = next(r for r in STATE.rows if r["id"] == scan_id)
    assert stored["status"] == "pending" and stored["verdict"] is None

    STATE.fail_next_update = True
    with api.connect() as work:
        with pytest.raises(FakePgError):
            worker.claim_id(work, scan_id)

    stored = next(r for r in STATE.rows if r["id"] == scan_id)
    assert stored["status"] == "pending"          # 没有半截 done
    assert stored["verdict"] is None
    assert stored["reason"] is None
    assert stored["processed_at"] is None
    assert stored["fill_factor"] == 0.66          # 读数原样未漂没

    with api.connect() as work:                   # 失败后可补处理
        assert worker.claim_id(work, scan_id)
        work.commit()
    stored = next(r for r in STATE.rows if r["id"] == scan_id)
    assert stored["status"] == "done"
    assert stored["verdict"] == "衰减"
    assert stored["fill_factor"] == 0.66


def test_drain_processes_every_pending_without_loss(client):
    h = auth(token(client, "scanner", "scan123456"))
    for code, ff in [("阵列E-串01", 0.74), ("阵列E-串02", 0.55)]:
        client.post("/api/logs", headers=h, json={
            "string_code": code, "voc_v": 40.0, "isc_a": 8.5, "fill_factor": ff,
        })
    with api.connect() as work:
        assert worker.drain(work) is True
        work.commit()
    pending = [r for r in STATE.rows if r["status"] == "pending"]
    assert pending == []
    table = by_code(client.get("/api/logs", headers=h).json())
    assert table["阵列E-串01"]["fill_factor"] == 0.74
    assert table["阵列E-串02"]["fill_factor"] == 0.55


FRONTEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "..", "frontend", "src", "App.vue")


def test_trap_modules_are_gone():
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("blank_ff.py", "h03_extra_trap.py", "h03_map_trap.py", "h03_queue_blank.py"):
        assert not os.path.exists(os.path.join(backend, name))


def test_frontend_binds_real_ff_on_table_and_card():
    """前端总表与详情卡都直接绑定 fill_factor，不再有置零/空白投影；
    提交表单仅扫描员可见。"""
    with open(FRONTEND, encoding="utf-8") as fh:
        vue = fh.read()
    assert "h03-trap-blank" not in vue
    assert "fill_factor === 0" not in vue
    assert "fill_factor == null" not in vue
    assert "{{ row.fill_factor }}" in vue         # 总表
    assert "{{ detail.fill_factor }}" in vue      # 详情卡
    assert 'v-if="isWriter"' in vue               # 观察员看不到写入口
