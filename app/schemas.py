"""请求 / 响应 Pydantic 模型。"""
from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

# 状态码：inside 内部点 / boundary 边界待判 / outside 流域外
PointStatus = Literal["inside", "boundary", "outside"]


class GridIn(BaseModel):
    grid_id: str = Field(..., min_length=1, max_length=128, description="网格唯一编号")
    geometry: dict[str, Any] = Field(..., description="Polygon 或 MultiPolygon 的 GeoJSON geometry")


class PublishRequest(BaseModel):
    expected_version: int | None = Field(
        None,
        ge=0,
        description="提交方所见的当前版号；首次发布传 null 或 0，之后传当前版号",
    )
    watershed: dict[str, Any] = Field(..., description="流域多边形 GeoJSON geometry")
    grids: list[GridIn] = Field(..., min_length=2, max_length=20, description="2~20 个带唯一编号的网格")


class ValidationIssue(BaseModel):
    type: Literal["overlap", "gap", "excess"]
    location: dict[str, float]
    representative_point: list[float]
    area: float
    message: str


class PublishResponse(BaseModel):
    version: int
    expected_version: int | None
    published_at: str
    grid_count: int
    status: Literal["published"] = "published"


class ErrorResponse(BaseModel):
    error: str
    detail: str
    issues: list[ValidationIssue] | None = None


class QueryPoint(BaseModel):
    point_id: str | None = Field(None, max_length=128, description="观测点编号，可选，便于结果对应")
    lon: float
    lat: float


class QueryRequest(BaseModel):
    version: int | None = Field(
        None, ge=1, description="固定查询版号；不传或传 null 表示当前版，一次查询内所有点同版"
    )
    points: list[QueryPoint] = Field(..., min_length=1, max_length=100, description="至多 100 个观测点")


class PointResult(BaseModel):
    point_id: str | None
    lon: float
    lat: float
    status: PointStatus
    grid_id: str | None = Field(None, description="内部点返回唯一网格编号；其余为 null")
    grid_ids: list[str] = Field(
        default_factory=list, description="边界点列出所有相接网格（待判）；域外为空"
    )


class QueryResponse(BaseModel):
    version: int = Field(..., description="本次查询实际使用的版号")
    is_current: bool
    results: list[PointResult]


class VersionInfo(BaseModel):
    version: int
    expected_version: int | None
    published_at: str
    grid_count: int


# ---- 网格压缩 ---------------------------------------------------------


class CompressPoint(BaseModel):
    point_id: str | None = Field(None, max_length=128, description="参照点编号，可选")
    lon: float = Field(..., allow_inf_nan=False, description="经度（或平面坐标 x）")
    lat: float = Field(..., allow_inf_nan=False, description="纬度（或平面坐标 y）")


class CompressRequest(BaseModel):
    """压缩请求；字段名同时接受常见同义写法（source_version / max_vertices 等）。"""

    model_config = ConfigDict(populate_by_name=True)

    version: int = Field(
        ...,
        ge=1,
        validation_alias=AliasChoices("version", "source_version"),
        description="要压缩的已发布版号",
    )
    tolerances: list[float] = Field(
        ...,
        min_length=2,
        max_length=8,
        validation_alias=AliasChoices(
            "tolerances", "candidate_tolerances", "candidate_tolerance"
        ),
        description="2~8 个候选简化容差，须为大于 0 的数值",
    )
    max_total_vertices: int = Field(
        ...,
        ge=0,
        validation_alias=AliasChoices(
            "max_total_vertices", "max_vertices", "vertex_limit", "max_vertex_count"
        ),
        description="压缩后整版网格顶点总数上限",
    )
    max_rel_area_dev: float = Field(
        ...,
        ge=0,
        validation_alias=AliasChoices(
            "max_rel_area_dev",
            "max_area_deviation",
            "max_relative_area_deviation",
            "area_deviation",
        ),
        description="每个网格相对原版面积允许的最大偏差（绝对值比例，如 0.05 表示 5%）",
    )
    reference_points: list[CompressPoint] = Field(
        default_factory=list,
        max_length=100,
        validation_alias=AliasChoices("reference_points", "ref_points", "points"),
        description="至多 100 个参照点，压缩前后落格结论必须一致",
    )

    @field_validator("tolerances")
    @classmethod
    def _positive_tolerances(cls, value: list[float]) -> list[float]:
        if any(not math.isfinite(t) or t <= 0 for t in value):
            raise ValueError("候选容差必须是大于 0 的有限数值")
        return value


class CandidateStatus(BaseModel):
    tolerance: float
    status: Literal["feasible", "rejected"]
    vertex_count: int | None = Field(
        None, description="该候选压缩后的顶点总数；未能完成构建时为 null"
    )
    reason: str | None = Field(None, description="被否决的原因码")
    detail: str | None = Field(None, description="被否决的说明")


class CompressResponse(BaseModel):
    status: Literal["compressed"] = "compressed"
    source_version: int = Field(..., description="压缩所依据的已发布版号")
    candidate_tolerance: float = Field(..., description="最终选中的候选容差")
    vertices_before: int
    vertices_after: int
    grid_count: int
    geojson: dict[str, Any] = Field(..., description="压缩后的网格 FeatureCollection")
    candidates: list[CandidateStatus] = Field(
        default_factory=list, description="全部候选容差的评估结果"
    )
