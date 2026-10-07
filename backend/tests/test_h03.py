"""H03 验收：填充因子读回不漂、总表与详情一致、观察员禁写、失败不留半空行。

走真实 Litestar ASGI（httpx 内存传输，不起网络），数据库为 conftest
注入的内存假库，保留事务提交/回滚语义。
"""
import pytest
from litestar.testing import TestClient

import api
from tests import fakedb


@pytest.fixture()
def client():
    with TestClient(api.app) as c:
        yield c


def login(client, username, password):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.is_success, res.text
    return res.json()["access_token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# --- 入库读数在总表与详情都不许漂没 -------------------------------------

def test_seed_ff_present_in_list_and_detail(client):
    token = login(client, "scanner", "scan123456")
    rows = client.get("/api/logs", headers=auth(token)).json()
    by_code = {r["string_code"]: r for r in rows}

    # 两个种子读数一个都不许变成空白/0/缺失。
    assert by_code["阵列A-串03"]["fill_factor"] == pytest.approx(0.78)
    assert by_code["阵列B-串11"]["fill_factor"] == pytest.approx(0.61)
    for r in rows:
        assert "fill_factor" in r and r["fill_factor"] is not None
        assert "ff_display" not in r  # 不允许再夹带洗白用的展示字段

    # 详情端点读数必须与总表逐字节一致。
    for code, ff in (("阵列A-串03", 0.78), ("阵列B-串11", 0.61)):
        sid = by_code[code]["id"]
        d = client.get(f"/api/logs/{sid}", headers=auth(token)).json()
        assert d["fill_factor"] == pytest.approx(ff)
        assert d["fill_factor"] == by_code[code]["fill_factor"]


@pytest.mark.parametrize("ff", [0.0, 0.61, 0.72, 0.78, 0.735])
def test_written_ff_roundtrips_unchanged(client, ff):
    token = login(client, "scanner", "scan123456")
    res = client.post(
        "/api/logs",
        headers=auth(token),
        json={"string_code": f"串-FF-{ff}", "voc_v": 40.0, "isc_a": 9.0,
              "fill_factor": ff},
    )
    assert res.status_code == 201, res.text
    created = res.json()
    # 创建回包本身不许抹零：0 必须以数值 0 回来，而不是 None/""。
    assert created["fill_factor"] == ff
    assert created["fill_factor"] is not None

    rows = client.get("/api/logs", headers=auth(token)).json()
    row = next(r for r in rows if r["id"] == created["id"])
    assert row["fill_factor"] == ff

    detail = client.get(f"/api/logs/{created['id']}", headers=auth(token)).json()
    assert detail["fill_factor"] == ff


def test_repeated_refreshes_keep_ff_stable(client):
    """翻页/反复刷新：读数不许凭空变成空白或零。"""
    token = login(client, "scanner", "scan123456")
    created = client.post(
        "/api/logs",
        headers=auth(token),
        json={"string_code": "刷新稳定串", "voc_v": 41.2, "isc_a": 9.1,
              "fill_factor": 0.77},
    ).json()

    seen = []
    for _ in range(4):
        rows = client.get("/api/logs", headers=auth(token)).json()
        row = next(r for r in rows if r["id"] == created["id"])
        seen.append(row["fill_factor"])
        d = client.get(f"/api/logs/{created['id']}", headers=auth(token)).json()
        seen.append(d["fill_factor"])
    assert seen == [0.77] * len(seen)
    assert all(v is not None and v != "" for v in seen)


# --- 观察员只读，不能写 ---------------------------------------------------

def test_reader_cannot_write_and_no_row_created(client):
    writer = login(client, "scanner", "scan123456")
    watcher = login(client, "watcher", "watch123456")

    before = client.get("/api/logs", headers=auth(writer)).json()
    res = client.post(
        "/api/logs",
        headers=auth(watcher),
        json={"string_code": "越权串", "voc_v": 40.0, "isc_a": 9.0,
              "fill_factor": 0.8},
    )
    assert res.status_code == 403
    after = client.get("/api/logs", headers=auth(writer)).json()
    # 被拒后库里不得多出任何行。
    assert {r["id"] for r in after} == {r["id"] for r in before}
    assert all(r["string_code"] != "越权串" for r in after)

    # 观察员仍然可以读总表和详情。
    assert res.status_code == 403
    ok = client.get("/api/logs", headers=auth(watcher))
    assert ok.status_code == 200
    some_id = ok.json()[0]["id"]
    assert client.get(f"/api/logs/{some_id}", headers=auth(watcher)).status_code == 200


def test_anonymous_write_rejected(client):
    res = client.post(
        "/api/logs",
        json={"string_code": "匿名串", "voc_v": 1, "isc_a": 1, "fill_factor": 0.8},
    )
    assert res.status_code == 401


# --- 半路失败不留半空行 ---------------------------------------------------

def test_failed_commit_leaves_no_half_row(client, monkeypatch):
    token = login(client, "scanner", "scan123456")
    before = client.get("/api/logs", headers=auth(token)).json()

    fakedb.FAIL_COMMIT_ON_CODE = "注定失败串"
    res = client.post(
        "/api/logs",
        headers=auth(token),
        json={"string_code": "注定失败串", "voc_v": 40.0, "isc_a": 9.0,
              "fill_factor": 0.79},
    )
    fakedb.FAIL_COMMIT_ON_CODE = None
    assert res.status_code >= 500

    after = client.get("/api/logs", headers=auth(token)).json()
    assert len(after) == len(before)
    assert all(r["string_code"] != "注定失败串" for r in after)
    # 不允许遗留 status=pending 但 FF 缺失/为零的半空行。
    assert all(
        (r["status"] != "pending") or (r["fill_factor"] not in (None, 0, ""))
        for r in after
    )


def test_detail_missing_returns_404(client):
    token = login(client, "scanner", "scan123456")
    res = client.get("/api/logs/999999", headers=auth(token))
    assert res.status_code == 404


# --- 工人处理后读数依旧完好 ----------------------------------------------

def test_after_worker_processes_ff_still_intact(client):
    import worker

    token = login(client, "scanner", "scan123456")
    created = client.post(
        "/api/logs",
        headers=auth(token),
        json={"string_code": "工人处理串", "voc_v": 38.0, "isc_a": 8.4,
              "fill_factor": 0.61},
    ).json()
    assert created["status"] == "pending"

    with worker.connect() as conn:
        assert worker.claim_id(conn, created["id"]) is True
        conn.commit()

    detail = client.get(f"/api/logs/{created['id']}", headers=auth(token)).json()
    assert detail["status"] == "done"
    assert detail["fill_factor"] == pytest.approx(0.61)
    assert detail["verdict"] == "衰减"

    # 已完成的行不会被再次认领。
    with worker.connect() as conn:
        assert worker.claim_id(conn, created["id"]) is False
