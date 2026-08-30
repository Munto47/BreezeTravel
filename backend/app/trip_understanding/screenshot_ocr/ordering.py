from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Iterable

from .models import RawOcrLine


@dataclass
class _RowCluster:
    lines: list[RawOcrLine] = field(default_factory=list)

    @property
    def center_y(self) -> float:
        return median(_geometry(line)[4] for line in self.lines)

    @property
    def min_y(self) -> float:
        return min(_geometry(line)[1] for line in self.lines)

    @property
    def max_y(self) -> float:
        return max(_geometry(line)[3] for line in self.lines)


def _geometry(line: RawOcrLine) -> tuple[float, float, float, float, float]:
    xs = [point[0] for point in line.bbox]
    ys = [point[1] for point in line.bbox]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    return min_x, min_y, max_x, max_y, (min_y + max_y) / 2


def _same_visual_row(line: RawOcrLine, cluster: _RowCluster) -> bool:
    _, min_y, _, max_y, center_y = _geometry(line)
    overlap = max(0.0, min(max_y, cluster.max_y) - max(min_y, cluster.min_y))
    line_height = max_y - min_y
    cluster_height = cluster.max_y - cluster.min_y
    overlap_ratio = overlap / min(line_height, cluster_height)
    center_tolerance = 0.35 * min(line_height, cluster_height)
    return overlap_ratio >= 0.5 or abs(center_y - cluster.center_y) <= center_tolerance


def sort_reading_order(lines: Iterable[RawOcrLine]) -> tuple[RawOcrLine, ...]:
    """Cluster by vertical alignment, then sort rows by y and lines by x."""

    ordered_candidates = sorted(
        lines,
        key=lambda line: (
            _geometry(line)[4],
            _geometry(line)[1],
            _geometry(line)[0],
            line.text,
            line.confidence,
        ),
    )
    clusters: list[_RowCluster] = []
    for line in ordered_candidates:
        compatible = [cluster for cluster in clusters if _same_visual_row(line, cluster)]
        if compatible:
            target = min(
                compatible,
                key=lambda cluster: (abs(_geometry(line)[4] - cluster.center_y), cluster.center_y),
            )
            target.lines.append(line)
        else:
            clusters.append(_RowCluster(lines=[line]))

    clusters.sort(key=lambda cluster: (cluster.center_y, cluster.min_y))
    result: list[RawOcrLine] = []
    for cluster in clusters:
        result.extend(
            sorted(
                cluster.lines,
                key=lambda line: (
                    _geometry(line)[0],
                    _geometry(line)[4],
                    _geometry(line)[1],
                    line.text,
                    line.confidence,
                ),
            )
        )
    return tuple(result)
