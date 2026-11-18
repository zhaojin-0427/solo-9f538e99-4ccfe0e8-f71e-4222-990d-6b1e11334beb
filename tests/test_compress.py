"""网格压缩 API 端到端测试。

覆盖：顶点下降且保持覆盖 / 落格结论、择优规则（顶点最少、同数取小容差）、
各种限制原因导致的无合格结果、只读性（原版与当前版不变）、入参校验。
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from tests.test_api import LEFT, RIGHT, WATERSHED, poly, publish_body


def dense_rect(x0: float, y0: float, x1: float, y1: float, step: float = 0.5):
    """按固定步长在每条边上打点的矩形，用于制造“顶点很多但形状简单”的网格。"""
    pts = []
    n = int(round((x1 - x0) / step))
    pts += [(x0 + i * step, y0) for i in range(n)]
    n = int(round((y1 - y0) / step))
    pts += [(x1, y0 + i * step) for i in range(n)]
    n = int(round((x1 - x0) / step))
    pts += [(x1 - i * step, y1) for i in range(n)]
    n = int(round((y1 - y0) / step))
    pts += [(x0, y1 - i * step) for i in range(n)]
    pts.append(pts[0])
    return poly(pts)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    import app.config as config
    import app.db as dbmod
    import app.main as main
    importlib.reload(config)
    importlib.reload(dbmod)
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c, main


@pytest.fixture()
def dense_client(client):
    """发布一个边长 0.5 打点的 10×10 流域（三格 T 形交界），返回客户端。"""
    c, main = client
    body = {
        "expected_version": None,
        "watershed": poly([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]),
        "grids": [
            {"grid_id": "A", "geometry": dense_rect(0, 0, 5, 5)},
            {"grid_id": "B", "geometry": dense_rect(5, 0, 10, 5)},
            {"grid_id": "C", "geometry": dense_rect(0, 5, 10, 10)},
        ],
    }
    r = c.post("/publish", json=body)
    assert r.status_code == 200, r.text
    return c, main


@pytest.fixture()
def wiggly_client(client):
    """发布一个公共边带锯齿的 10×10 两格流域（锯齿幅度 0.1）。

    锯齿 ⇒ 压缩会改变面积、也会移动公共边附近点的归属，
    便于构造面积偏差 / 参照点不一致的用例。
    """
    c, main = client
    n = 41
    shared = [(5 + (0.1 if i % 2 else -0.1), 10 * i / (n - 1)) for i in range(n)]
    shared[0], shared[-1] = (5, 0), (5, 10)
    left = poly([(0, 0), (5, 0)] + shared[1:] + [(0, 10), (0, 0)])
    right = poly([(5, 0), (10, 0), (10, 10), (5, 10)] + list(reversed(shared))[1:-1] + [(5, 0)])
    r = c.post("/publish", json={
        "expected_version": None,
        "watershed": poly([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]),
        "grids": [
            {"grid_id": "A", "geometry": left},
            {"grid_id": "B", "geometry": right},
        ],
    })
    assert r.status_code == 200, r.text
    return c, main


def compress_body(**overrides):
    body = {
        "version": 1,
        "tolerances": [0.1, 0.5, 2.0],
        "max_total_vertices": 1000,
        "max_rel_area_dev": 0.05,
        "reference_points": [
            {"point_id": "a", "lon": 2, "lat": 2},
            {"point_id": "b", "lon": 7, "lat": 2},
            {"point_id": "c", "lon": 5, "lat": 8},
            {"point_id": "out", "lon": 20, "lat": 20},
        ],
    }
    body.update(overrides)
    return body


# ---------- 主流程 ----------

def test_compress_reduces_vertices_and_keeps_classification(dense_client):
    c, main = dense_client
    before = c.post("/query", json={"points": [
        {"lon": 2, "lat": 2}, {"lon": 7, "lat": 2}, {"lon": 5, "lat": 8},
        {"lon": 5, "lat": 5}, {"lon": 20, "lat": 20},
    ]}).json()["results"]

    r = c.post("/compress", json=compress_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "compressed"
    assert body["source_version"] == 1
    assert body["vertices_after"] < body["vertices_before"]
    assert body["vertices_before"] == 140
    assert body["grid_count"] == 3
    assert body["candidate_tolerance"] in (0.1, 0.5, 2.0)

    # 压缩结果的顶点数自洽，且与原版落格结论一致
    feats = body["geojson"]["features"]
    assert body["geojson"]["type"] == "FeatureCollection"
    assert sorted(f["properties"]["grid_id"] for f in feats) == ["A", "B", "C"]
    assert sum(f["properties"]["vertex_count"] for f in feats) == body["vertices_after"]

    after = c.post("/query", json={"points": [
        {"lon": 2, "lat": 2}, {"lon": 7, "lat": 2}, {"lon": 5, "lat": 8},
        {"lon": 5, "lat": 5}, {"lon": 20, "lat": 20},
    ]}).json()["results"]
    assert [
        (x["status"], x["grid_id"], sorted(x["grid_ids"])) for x in before
    ] == [
        (x["status"], x["grid_id"], sorted(x["grid_ids"])) for x in after
    ]


def test_compress_is_read_only(dense_client):
    c, main = dense_client
    before_versions = c.get("/versions").json()
    before_snapshot = main.db.load_snapshot(1)
    r = c.post("/compress", json=compress_body())
    assert r.status_code == 200
    assert c.get("/versions").json() == before_versions
    assert c.get("/health").json()["current_version"] == 1
    after_snapshot = main.db.load_snapshot(1)
    assert after_snapshot[0].equals(before_snapshot[0])
    assert [(gid, g.wkt) for gid, g in after_snapshot[1]] == [
        (gid, g.wkt) for gid, g in before_snapshot[1]
    ]


def test_compress_works_on_old_version(dense_client):
    c, main = dense_client
    # 以当前版 1 发布第 2 版（不同网格划分），压缩仍按版号 1 取数
    body = publish_body  # noqa: F841  (仅为可读性)
    r2 = c.post("/publish", json={
        "expected_version": 1,
        "watershed": poly([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]),
        "grids": [
            {"grid_id": "D", "geometry": dense_rect(0, 0, 10, 5)},
            {"grid_id": "E", "geometry": dense_rect(0, 5, 10, 10)},
        ],
    })
    assert r2.status_code == 200
    r = c.post("/compress", json=compress_body(version=1))
    assert r.status_code == 200, r.text
    assert r.json()["source_version"] == 1
    assert r.json()["grid_count"] == 3
    assert c.get("/health").json()["current_version"] == 2


def test_vertex_limit_selects_smallest_feasible(dense_client):
    c, main = dense_client
    r = c.post("/compress", json=compress_body(max_total_vertices=20))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vertices_after"] <= 20
    feasible = [x for x in body["candidates"] if x["status"] == "feasible"]
    assert body["vertices_after"] == min(x["vertex_count"] for x in feasible)
    # 同顶点数时取较小容差
    same = [x for x in feasible if x["vertex_count"] == body["vertices_after"]]
    assert body["candidate_tolerance"] == min(x["tolerance"] for x in same)


def test_candidate_order_in_request_does_not_change_result(dense_client):
    c, main = dense_client
    a = c.post("/compress", json=compress_body(tolerances=[0.1, 0.5, 2.0])).json()
    b = c.post("/compress", json=compress_body(tolerances=[2.0, 0.5, 0.1])).json()
    assert a["candidate_tolerance"] == b["candidate_tolerance"]
    assert a["vertices_after"] == b["vertices_after"]
    assert a["geojson"] == b["geojson"]


# ---------- 无合格结果：逐项说明原因 ----------

def test_no_feasible_when_vertex_limit_too_small(dense_client):
    c, main = dense_client
    r = c.post("/compress", json=compress_body(max_total_vertices=4))
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "NO_FEASIBLE_CANDIDATE"
    assert "geojson" not in body
    assert body["source_version"] == 1
    assert body["vertices_before"] == 140
    reasons = {x["reason"] for x in body["candidates"]}
    assert "VERTEX_LIMIT" in reasons
    assert all(x["status"] == "rejected" for x in body["candidates"])
    assert all(x["detail"] for x in body["candidates"])


def test_no_feasible_when_area_deviation_too_strict(wiggly_client):
    c, main = wiggly_client
    # 拉直锯齿会使每格面积变化约 5e-4，上限 1e-4 时没有候选能通过
    r = c.post("/compress", json=compress_body(
        tolerances=[0.05, 0.5, 5.0], max_rel_area_dev=1e-4
    ))
    assert r.status_code == 422
    body = r.json()
    reasons = {x["reason"] for x in body["candidates"]}
    assert "AREA_DEVIATION" in reasons
    assert all(x["status"] == "rejected" for x in body["candidates"])
    assert "geojson" not in body


def test_area_deviation_limit_accepts_consistent_candidate(wiggly_client):
    c, main = wiggly_client
    r = c.post("/compress", json=compress_body(
        tolerances=[0.05, 0.5, 5.0],
        max_rel_area_dev=0.001,
        reference_points=[
            {"point_id": "a", "lon": 2, "lat": 2},
            {"point_id": "b", "lon": 8, "lat": 2},
            {"point_id": "out", "lon": 20, "lat": 20},
        ],
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vertices_after"] < body["vertices_before"]
    assert body["candidate_tolerance"] == 0.05 or body["vertices_after"] < 20


def test_no_feasible_when_reference_point_changes(wiggly_client):
    c, main = wiggly_client
    # (4.9, 5) 位于公共边左侧、距锯齿线约 0.1；拉直后落到公共边上
    r = c.post("/compress", json=compress_body(
        tolerances=[0.01, 5.0],
        max_rel_area_dev=1.0,
        reference_points=[
            {"point_id": "near-edge", "lon": 4.9, "lat": 5},
            {"point_id": "a", "lon": 2, "lat": 2},
        ],
    ))
    assert r.status_code == 422
    body = r.json()
    detail = " ".join(x["detail"] or "" for x in body["candidates"])
    assert "near-edge" in detail
    assert any(x["reason"] == "REFERENCE_POINT" for x in body["candidates"])
    assert "geojson" not in body


def test_no_feasible_when_nothing_to_simplify(client):
    c, main = client
    # 简单网格没有内部噪声可去掉，任何容差都无法降低顶点数
    assert c.post("/publish", json=publish_body()).status_code == 200
    r = c.post("/compress", json={
        "version": 1,
        "tolerances": [0.1, 1.0],
        "max_total_vertices": 100,
        "max_rel_area_dev": 1.0,
        "reference_points": [{"lon": 1, "lat": 2}],
    })
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "NO_FEASIBLE_CANDIDATE"
    assert all(x["reason"] == "NOT_REDUCED" for x in body["candidates"])
    assert "geojson" not in body


def test_version_not_found(client):
    c, main = client
    c.post("/publish", json=publish_body())
    r = c.post("/compress", json=compress_body(version=9))
    assert r.status_code == 404
    assert r.json()["error"] == "VERSION_NOT_FOUND"


# ---------- 入参校验 ----------

@pytest.mark.parametrize("overrides", [
    {"tolerances": [0.1]},                       # 少于 2 个
    {"tolerances": [0.1] * 9},                   # 多于 8 个
    {"tolerances": [0.0, 0.1]},                  # 非正容差
    {"tolerances": [0.1, -1.0]},                 # 负容差
    {"tolerances": [0.1, 1] * 4, "reference_points": [{"lon": 1, "lat": 1}] * 101},
    {"max_rel_area_dev": -0.1},                  # 偏差上限为负
    {"max_total_vertices": -1},                  # 顶点上限为负
    {"tolerances": [0.1, 0.2], "version": 0},    # 版号非法
])
def test_invalid_request_rejected(client, overrides):
    c, main = client
    c.post("/publish", json=publish_body())
    r = c.post("/compress", json=compress_body(**overrides))
    assert r.status_code == 422


def test_bad_geojson_in_reference_points_rejected(client):
    c, main = client
    c.post("/publish", json=publish_body())
    r = c.post("/compress", json=compress_body(
        reference_points=[{"lon": "abc", "lat": 1}]
    ))
    assert r.status_code == 422


def test_alias_field_names_accepted(dense_client):
    c, main = dense_client
    body = {
        "source_version": 1,
        "candidate_tolerances": [0.1, 0.5],
        "max_vertices": 20,
        "max_area_deviation": 0.05,
        "ref_points": [{"lon": 2, "lat": 2}, {"lon": 7, "lat": 2}],
    }
    r = c.post("/compress", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["vertices_after"] <= 20
