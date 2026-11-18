# 流域防汛网格服务

基于 **FastAPI + Shapely + SQLite** 的防汛网格整版发布与观测点落格查询 API。

## 业务规则

### 发布（`POST /publish`）

- 提交一个流域多边形和 **2～20 个**带唯一编号的网格 GeoJSON（Polygon 或 MultiPolygon）。
- 网格之间**内部不得重叠**（共边、共点的相切允许）。
- 网格合并后须**恰好覆盖**流域：既不能有缺口（`gap`），也不能越界（`excess`）。
- 任何重叠 / 缺口 / 越界都会在响应 `issues` 中给出问题位置（包围盒、代表点、面积），**整版拒绝**，当前版不变。
- 请求携带提交方**所见版号** `expected_version`（首次发布传 `null` 或 `0`）。两个并发改版只有一个能按该版号发布，另一个收到 `409 VERSION_CONFLICT`。
- 发布成功后版本号递增；旧版网格整版保留，永不改写。

### 查询（`POST /query`）
- 一次至多 **100 个**观测点；一次查询固定使用**同一版本**（`version` 不传 = 当前版）。
- 判定结果：
  - `inside`：点在某网格内部，返回唯一 `grid_id`；
  - `boundary`：点落在网格 / 流域边界上（含角点、洞边），列出所有相接网格，`grid_id` 为 `null`，标记为**待判**；
  - `outside`：流域外，网格列表为空。
- 指定的版本不存在返回 `404 VERSION_NOT_FOUND`；尚无任何已发布版本时查当前版返回 `404 NO_PUBLISHED_VERSION`。
- 旧版数据持久化在 SQLite 中，**进程 / 容器重启后仍可查询**。

### 压缩（`POST /compress`）

面向带宽有限的现场终端：在**不改变观测点落格结论**的前提下压缩已发布网格的顶点数。

请求字段：

| 字段 | 说明 |
| --- | --- |
| `version` | 要压缩的已发布版号（也接受 `source_version`） |
| `tolerances` | **2～8 个**候选简化容差，必须大于 0 |
| `max_total_vertices` | 压缩后整版网格顶点总数上限 |
| `max_rel_area_dev` | 每格相对原版面积允许的最大偏差（比例，`0.05` 表示 5%） |
| `reference_points` | 至多 **100 个**参照点（`lon` / `lat` / 可选 `point_id`） |

每个候选容差都按下列限制逐项复核，任何一项不通过即整个候选作废：

1. **流域外边界原样保留**（外部边界顶点不丢失、不替换）；
2. 网格**内部无新增重叠**、合并后**无缺口、无越界**（复用发布时的整版校验）；
3. 每格相对面积偏差不超过 `max_rel_area_dev`；
4. 每个参照点的 `inside / boundary / outside` 状态、以及相接网格编号集合与原版**完全一致**；
5. 顶点总数不超过 `max_total_vertices`，且**低于原版**。

在全部合格候选中取**顶点最少**者，顶点数相同取**较小容差**。结果只读：不发布新版本，原版与当前版均不变。

```bash
curl -X POST http://localhost:8000/compress \
  -H 'Content-Type: application/json' \
  -d @examples/compress.json
```

成功响应（`200`）：

```json
{
  "status": "compressed",
  "source_version": 1,
  "candidate_tolerance": 0.05,
  "vertices_before": 86,
  "vertices_after": 8,
  "grid_count": 2,
  "geojson": {
    "type": "FeatureCollection",
    "features": [
      {"type": "Feature", "properties": {"grid_id": "L", "vertex_count": 4},
       "geometry": {"type": "Polygon", "coordinates": [[[0,0],[2,0],[2,4],[0,4],[0,0]]]}}
    ]
  },
  "candidates": [
    {"tolerance": 0.05, "status": "feasible", "vertex_count": 8, "reason": null, "detail": null},
    {"tolerance": 0.2, "status": "rejected", "vertex_count": null, "reason": "AREA_DEVIATION", "detail": "…"}
  ]
}
```

无合格候选返回 `422 NO_FEASIBLE_CANDIDATE`，逐条说明每个容差被否决的原因，**不返回任何部分网格**：

```json
{
  "error": "NO_FEASIBLE_CANDIDATE",
  "detail": "没有满足全部限制的候选。…",
  "source_version": 1,
  "vertices_before": 86,
  "candidates": [
    {"tolerance": 0.05, "status": "rejected", "vertex_count": 86, "reason": "NOT_REDUCED", "detail": "…"},
    {"tolerance": 0.5, "status": "rejected", "vertex_count": null, "reason": "VERTEX_LIMIT", "detail": "…"}
  ]
}
```

原因码：`NOT_REDUCED`（顶点未低于原版）、`VERTEX_LIMIT`（超总顶点上限）、`AREA_DEVIATION`（单格面积偏差超限）、`REFERENCE_POINT`（参照点落格结论改变）、`OVERLAP` / `GAP` / `EXCESS`（覆盖关系被破坏）、`BOUNDARY_CHANGED`（流域外边界被改动）、`TOPOLOGY` / `INVALID_GRID` / `UNASSIGNED_FACE` / `MISSING_GRID`（简化后无法重建网格）。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

- 服务地址：http://localhost:8000
- 交互式文档（可直接提交和查询）：http://localhost:8000/docs
- SQLite 数据保存在命名卷 `watershed-data`（容器内 `/data/watershed.db`），重建容器不丢数据。

## 本地运行（无 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
DATABASE_PATH=data/watershed.db .venv/bin/uvicorn app.main:app --reload
```

## 请求示例

发布第一版：

```bash
curl -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d @examples/publish_v1.json
```

携带所见版号发布第二版（并发时只有一个成功，另一个返回 409）：

```bash
curl -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d @examples/publish_v2.json
```

批量查询（可传 `"version": 1` 固定查旧版，不传则查当前版）：

```bash
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d @examples/query.json
```

错误响应（重叠 / 缺口，HTTP 422）示例：

```json
{
  "error": "COVERAGE_INVALID",
  "detail": "网格存在重叠或未恰好覆盖流域，整版拒绝（问题位置见 issues）",
  "issues": [
    {
      "type": "overlap",
      "location": {"minx": 1.0, "miny": 0.0, "maxx": 2.0, "maxy": 4.0},
      "representative_point": [1.5, 2.0],
      "area": 4.0,
      "message": "网格 L 与 X 内部重叠，重叠面积 4"
    }
  ]
}
```

## 其他端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查与当前版号 |
| GET | `/versions` | 全部已发布版本清单 |
| POST | `/compress` | 按候选容差压缩已发布网格（只读，不产生新版本） |
| GET | `/docs` | Swagger UI，可在线提交 / 查询 |

## 设计说明

- **乐观锁**：`meta.current_version` 记录当前版。发布在单条 `BEGIN IMMEDIATE` 事务内比对 `expected_version` 与当前版，不一致即回滚并返回 409；应用层另用写锁串行化同进程并发，SQLite `busy_timeout` 处理跨进程锁等待。
- **持久化**：几何以 WKB 存入 `versions` / `grids` 表，WAL 模式；每个版本独立行组，发布新版本不修改旧版任何行。
- **容差**：面积比较使用 `AREA_TOL_REL × 流域面积`（默认 `1e-9`）吸收浮点噪音；点查询对距边界极近（`POINT_EPS_REL`，默认 `1e-9` × 外接矩形对角线）的点兜底判为待判，避免浮点抖动把边界点误判为域外。
- **压缩的拓扑保持**：只简化网格之间的内部公共边界（外边界整体保留）。先把所有网格边界并集减去流域边界得到内部界线并 `linemerge` 成长线，按容差做保拓扑简化（端点固定），再与流域外边界一起 `polygonize` 重建面片；面片用**原版**网格按代表点归属、同编号合并成新网格。因此简化后仍然互不重叠且恰好覆盖流域，不会产生新网格或改变编号。
- 坐标系按平面坐标处理（经纬度直接作为 x/y），面积单位与输入坐标单位一致；如需米制面积请在入库前投影。

## 测试

```bash
.venv/bin/python -m pytest tests/ -v
```

覆盖：首版发布、版号乐观锁冲突、并发发布仅一个成功、网格数上下限、编号重复、重叠 / 缺口 / 越界定位、无效几何拒绝、失败发布不改当前版、内部 / 边界 / 角点 / 洞边 / 域外判定、100 点上限、旧版查询、版本不存在、重启后旧版仍可查；压缩的顶点下降与落格结论保持、只读性、旧版压缩、择优规则（顶点最少 / 同数取小容差）、顶点上限 / 面积偏差 / 参照点 / 无可简化等各类无合格结果及原因、入参校验与字段别名。
