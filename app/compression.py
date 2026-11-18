"""网格压缩：在保持落格结论的前提下减少整版网格顶点。

压缩只处理网格之间的内部公共边界（共享边），流域外边界整体保留、不做简化：

1. 取全部网格边界的并集，减去流域外边界，得到内部界线（已被节点切分）；
2. 对每条内部界线按候选容差做保拓扑简化，端点固定（简化不会移动线段两端）；
3. 把流域外边界与简化后的内部界线重新合成平面图，`polygonize` 得到面片；
4. 用面片代表点按**原版**网格落格，确定面片归属；同一网格的面片合并为新网格；
5. 对新整版逐项复核：覆盖关系（无新增重叠 / 缺口 / 越界）、流域外边界、
   每格面积相对偏差、参照点落格结论，任何一项不满足即整个候选作废。

只有同时满足顶点上限、顶点数低于原版、且全部复核通过的候选才参与择优。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from shapely import STRtree
from shapely.geometry import MultiLineString, Polygon, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, polygonize, unary_union
from shapely.validation import explain_validity, make_valid

from .geometry import GeometryError, classify_point, validate_submission

# 面片代表点落格时复用的浮点兜底比例（与查询一致，由调用方传入绝对距离）
DEFAULT_EPS_REL = 1e-9


class CandidateRejected(Exception):
    """某个候选容差无法满足压缩限制。"""

    def __init__(self, reason: str, detail: str, vertex_count: int | None = None) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.vertex_count = vertex_count


@dataclass
class Candidate:
    tolerance: float
    accepted: bool
    vertex_count: int | None = None
    reason: str | None = None
    detail: str | None = None
    grids: list[tuple[str, BaseGeometry]] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tolerance": self.tolerance,
            "status": "feasible" if self.accepted else "rejected",
            "vertex_count": self.vertex_count,
            "reason": self.reason,
            "detail": self.detail,
        }


# ---- 顶点计数 ---------------------------------------------------------


def _open_ring_vertices(coords: Sequence[tuple[float, float]]) -> int:
    """闭合环的顶点数（首尾重复坐标只计一次）。"""
    n = len(coords)
    if n >= 2 and coords[0] == coords[-1]:
        n -= 1
    return max(n, 0)


def count_vertices(geom: BaseGeometry) -> int:
    """Polygon / MultiPolygon 的顶点总数（洞边一并计入）。"""
    if geom.geom_type == "Polygon":
        return _open_ring_vertices(geom.exterior.coords) + sum(
            _open_ring_vertices(ring.coords) for ring in geom.interiors
        )
    if geom.geom_type == "MultiPolygon":
        return sum(count_vertices(part) for part in geom.geoms)
    return 0


def total_vertices(grids: Iterable[tuple[str, BaseGeometry]]) -> int:
    return sum(count_vertices(geom) for _, geom in grids)


# ---- 界线处理 ---------------------------------------------------------


def _iter_lines(geom: BaseGeometry) -> Iterable[BaseGeometry]:
    if geom.is_empty:
        return
    if geom.geom_type in ("LineString", "LinearRing"):
        yield geom
    elif hasattr(geom, "geoms"):
        for part in geom.geoms:
            yield from _iter_lines(part)


def _merge_lines(geom: BaseGeometry) -> BaseGeometry:
    """把同一几何中的共线碎段合并成尽量长的线，避免重复简化接缝。"""
    lines = list(_iter_lines(geom))
    if not lines:
        return MultiLineString([])
    if len(lines) == 1:
        return lines[0]
    merged = linemerge(MultiLineString(lines))
    return merged


def _simplify_internal(internal: BaseGeometry, tolerance: float) -> list[BaseGeometry]:
    """逐条简化内部界线；端点不动，保证仍与流域边界 / 其他界线相接。

    先做一次 linemerge：多网格并集 / 差集会按节点把界线切成碎段，
    直接对碎段简化没有效果（碎段只有两个端点）。
    """
    simplified: list[BaseGeometry] = []
    for line in _iter_lines(_merge_lines(internal)):
        part = line.simplify(tolerance, preserve_topology=True)
        simplified.extend(_iter_lines(part))
    return simplified


def _outer_vertex_set(geom: BaseGeometry, ndigits: int = 9) -> set[tuple[float, float]]:
    out: set[tuple[float, float]] = set()
    lines = _iter_lines(geom.boundary)
    for line in lines:
        for x, y in line.coords:
            out.add((round(x, ndigits), round(y, ndigits)))
    return out


def _boundary_preserved(watershed: BaseGeometry, new_union: BaseGeometry) -> bool:
    """流域外边界必须原样保留（顶点集合不被替换 / 丢失）。"""
    original = _outer_vertex_set(watershed)
    if not original.issubset(_outer_vertex_set(new_union)):
        return False
    return True


# ---- 单候选构建 -------------------------------------------------------


def build_candidate(
    watershed: BaseGeometry,
    grids: list[tuple[str, BaseGeometry]],
    tolerance: float,
    max_rel_area_dev: float,
    reference_points: Sequence[dict[str, Any]],
    area_tol_rel: float,
    eps: float,
) -> list[tuple[str, BaseGeometry]]:
    """按给定容差压缩，复核全部限制；任何一项不满足即抛 CandidateRejected。"""
    order = [grid_id for grid_id, _ in grids]
    original_by_id = dict(grids)
    original_total = total_vertices(grids)

    outer = watershed.boundary
    all_boundaries = unary_union([geom.boundary for _, geom in grids])
    internal = all_boundaries.difference(outer)
    if internal.is_empty:
        # 没有内部界线可简化，顶点数不可能下降
        raise CandidateRejected(
            "NOT_REDUCED",
            "网格之间没有可简化的内部公共边界，顶点数不会低于原版",
            original_total,
        )

    simplified = _simplify_internal(internal, tolerance)
    arrangement = unary_union([outer, *simplified])

    faces: list[Polygon] = []
    area_tol = max(watershed.area * area_tol_rel, 0.0)
    for face in polygonize(arrangement):
        if face.is_empty or face.area <= 0.0:
            continue
        if not face.is_valid:
            face = make_valid(face)
        if face.geom_type == "MultiPolygon":
            parts = list(face.geoms)
        elif face.geom_type == "Polygon":
            parts = [face]
        else:
            raise CandidateRejected(
                "TOPOLOGY", "简化后无法由界线重建面片，候选作废"
            )
        for part in parts:
            if part.area <= 0.0:
                continue
            outside = part.difference(watershed)
            if outside.area > area_tol:
                raise CandidateRejected(
                    "EXCESS",
                    f"容差 {tolerance:g} 简化后网格越出流域，越界面积 {outside.area:g}",
                )
            faces.append(part)

    if not faces:
        raise CandidateRejected("TOPOLOGY", "简化后没有生成任何有效面片，候选作废")

    # 面片归属：用原版网格判定面片代表点，归属不唯一则无法保持落格结论
    by_grid: dict[str, list[Polygon]] = {grid_id: [] for grid_id in order}
    for face in faces:
        rep = face.representative_point()
        verdict = classify_point(rep.x, rep.y, watershed, grids, eps)
        if verdict["status"] != "inside" or verdict["grid_id"] is None:
            raise CandidateRejected(
                "UNASSIGNED_FACE",
                f"容差 {tolerance:g} 简化产生的面片无法唯一归属到原网格"
                f"（代表点 [{rep.x:g}, {rep.y:g}] 判定为 {verdict['status']}）",
            )
        by_grid[verdict["grid_id"]].append(face)

    new_grids: list[tuple[str, BaseGeometry]] = []
    for grid_id in order:
        parts = by_grid[grid_id]
        if not parts:
            raise CandidateRejected(
                "MISSING_GRID", f"容差 {tolerance:g} 简化后网格 {grid_id} 消失，候选作废"
            )
        geom = unary_union(parts)
        if not geom.is_valid:
            raise CandidateRejected(
                "INVALID_GRID",
                f"容差 {tolerance:g} 简化后网格 {grid_id} 几何无效："
                f"{explain_validity(geom)}",
            )
        if geom.geom_type not in ("Polygon", "MultiPolygon"):
            raise CandidateRejected(
                "INVALID_GRID",
                f"容差 {tolerance:g} 简化后网格 {grid_id} 不是面几何",
            )
        new_grids.append((grid_id, geom))

    # 1) 整版覆盖复核：内部无新增重叠、无新增缺口、无越界
    issues = validate_submission(watershed, new_grids, area_tol_rel)
    if issues:
        first = issues[0]
        reason = {"overlap": "OVERLAP", "gap": "GAP", "excess": "EXCESS"}[
            first["type"]
        ]
        raise CandidateRejected(
            reason,
            f"容差 {tolerance:g} 简化后覆盖关系被破坏：{first['message']}",
        )

    new_union = unary_union([geom for _, geom in new_grids])

    # 2) 流域外边界原样保留
    if not _boundary_preserved(watershed, new_union):
        raise CandidateRejected(
            "BOUNDARY_CHANGED", f"容差 {tolerance:g} 简化改动了流域外边界"
        )

    # 3) 每格相对面积偏差
    for grid_id, geom in new_grids:
        original_area = original_by_id[grid_id].area
        deviation = abs(geom.area - original_area) / original_area
        # 绝对容差 1e-12 仅吸收浮点误差；上限为 0 时仍要求面积严格不变
        if deviation > 0.0 and deviation > max_rel_area_dev + 1e-12:
            raise CandidateRejected(
                "AREA_DEVIATION",
                f"容差 {tolerance:g} 下网格 {grid_id} 相对面积偏差 "
                f"{deviation:.6g} 超过上限 {max_rel_area_dev:g}",
            )

    # 4) 参照点：内部 / 边界 / 域外状态及相接网格编号必须与原版一致
    for point in reference_points:
        expected = classify_point(point["lon"], point["lat"], watershed, grids, eps)
        actual = classify_point(
            point["lon"], point["lat"], watershed, new_grids, eps
        )
        if (
            expected["status"] != actual["status"]
            or expected["grid_id"] != actual["grid_id"]
            or sorted(expected["grid_ids"]) != sorted(actual["grid_ids"])
        ):
            label = point.get("point_id") or f"[{point['lon']:g}, {point['lat']:g}]"
            raise CandidateRejected(
                "REFERENCE_POINT",
                f"容差 {tolerance:g} 下参照点 {label} 落格结论改变："
                f"原版 {expected['status']} {expected['grid_ids']} → "
                f"压缩后 {actual['status']} {actual['grid_ids']}",
            )

    count = total_vertices(new_grids)
    if count >= original_total:
        raise CandidateRejected(
            "NOT_REDUCED",
            f"容差 {tolerance:g} 顶点数 {count} 未低于原版 {original_total}",
            count,
        )
    return new_grids


# ---- 择优 -------------------------------------------------------------


def compress(
    watershed: BaseGeometry,
    grids: list[tuple[str, BaseGeometry]],
    tolerances: Sequence[float],
    max_total_vertices: int,
    max_rel_area_dev: float,
    reference_points: Sequence[dict[str, Any]],
    area_tol_rel: float,
    point_eps_rel: float = DEFAULT_EPS_REL,
) -> tuple[list[tuple[str, BaseGeometry]], Candidate, list[Candidate]]:
    """逐一评估候选容差，返回最优结果与全部候选状态。

    合格条件：复核通过、顶点总数不超过上限、且低于原版。
    在合格候选中取顶点最少者，同数取较小容差。
    """
    diag = max(
        watershed.bounds[2] - watershed.bounds[0],
        watershed.bounds[3] - watershed.bounds[1],
    )
    eps = diag * point_eps_rel

    original_total = total_vertices(grids)
    results: list[Candidate] = []
    for tolerance in sorted(set(tolerances)):
        try:
            new_grids = build_candidate(
                watershed,
                grids,
                tolerance,
                max_rel_area_dev,
                reference_points,
                area_tol_rel,
                eps,
            )
        except CandidateRejected as exc:
            results.append(
                Candidate(
                    tolerance=tolerance,
                    accepted=False,
                    vertex_count=exc.vertex_count,
                    reason=exc.reason,
                    detail=exc.detail,
                )
            )
            continue
        count = total_vertices(new_grids)
        if count > max_total_vertices:
            results.append(
                Candidate(
                    tolerance=tolerance,
                    accepted=False,
                    vertex_count=count,
                    reason="VERTEX_LIMIT",
                    detail=(
                        f"容差 {tolerance:g} 顶点数 {count} 超过总顶点上限 "
                        f"{max_total_vertices}"
                    ),
                )
            )
            continue
        results.append(
            Candidate(
                tolerance=tolerance,
                accepted=True,
                vertex_count=count,
                grids=new_grids,
            )
        )

    feasible = [c for c in results if c.accepted]
    if not feasible:
        raise NoFeasibleCandidate(original_total, results)

    best = min(feasible, key=lambda c: (c.vertex_count, c.tolerance))
    assert best.grids is not None
    return best.grids, best, results


class NoFeasibleCandidate(Exception):
    """所有候选容差都无法满足限制。"""

    def __init__(self, original_total: int, candidates: list[Candidate]) -> None:
        self.original_total = original_total
        self.candidates = candidates
        reasons = "；".join(
            f"容差 {c.tolerance:g}：{c.detail}" for c in candidates
        )
        super().__init__(f"没有满足全部限制的候选。{reasons}")


# ---- 输出 -------------------------------------------------------------


def to_feature_collection(
    grids: list[tuple[str, BaseGeometry]]
) -> dict[str, Any]:
    features = []
    for grid_id, geom in grids:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "grid_id": grid_id,
                    "vertex_count": count_vertices(geom),
                },
                "geometry": mapping(geom),
            }
        )
    return {"type": "FeatureCollection", "features": features}
