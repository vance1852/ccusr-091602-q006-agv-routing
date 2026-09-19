"""仓库拓扑：有向图、消防区、封闭路段与受限寻路。

边 (a, b) 表示资源 a 与 b 相邻、车辆可依次通过。
寻路只用于**尚未通过的路段**的重新规划；原始路线与理由由服务层保留。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .models import FireZone, Resource, ResourceKind


@dataclass
class Topology:
    resources: dict[str, Resource] = field(default_factory=dict)
    adj: dict[str, set[str]] = field(default_factory=dict)
    fire_zones: dict[str, FireZone] = field(default_factory=dict)

    # ---- 构建 --------------------------------------------------------------

    def add_resource(self, resource: Resource) -> None:
        self.resources[resource.id] = resource
        self.adj.setdefault(resource.id, set())

    def connect(self, a: str, b: str, bidirectional: bool = True) -> None:
        if a not in self.resources or b not in self.resources:
            raise KeyError(f"unknown resource in edge ({a}, {b})")
        self.adj[a].add(b)
        if bidirectional:
            self.adj[b].add(a)

    def add_fire_zone(self, zone: FireZone) -> None:
        if zone.exit_resource not in zone.resources:
            raise ValueError("fire zone exit must belong to the zone")
        self.fire_zones[zone.id] = zone
        for rid in zone.resources:
            r = self.resources.get(rid)
            if r is not None:
                self.resources[rid] = Resource(
                    rid, r.kind, zone.id, r.tags | {"fire"}, r.closed
                )

    # ---- 状态 --------------------------------------------------------------

    def close(self, resource_id: str) -> None:
        r = self.resources[resource_id]
        self.resources[resource_id] = Resource(
            resource_id, r.kind, r.zone, r.tags, closed=True
        )

    def is_closed(self, resource_id: str) -> bool:
        return self.resources[resource_id].closed

    def fire_zone_of(self, resource_id: str) -> Optional[FireZone]:
        r = self.resources.get(resource_id)
        if r is None or r.zone is None:
            return None
        return self.fire_zones.get(r.zone)

    def in_fire_zone(self, resource_id: str) -> bool:
        return self.fire_zone_of(resource_id) is not None

    # ---- 寻路 --------------------------------------------------------------

    def _passable(self, rid: str, forbid: Iterable[str]) -> bool:
        r = self.resources[rid]
        return not r.closed and rid not in forbid

    def shortest_path(
        self,
        src: str,
        dst: str,
        *,
        forbid: Iterable[str] = (),
        capabilities: Optional[set[str]] = None,
    ) -> Optional[list[str]]:
        """Dijkstra；返回含起止点的资源序列，不可达返回 None。

        带 capability 标签（``cap:<name>``）的资源要求车辆具备该能力。
        """
        forbid = set(forbid)
        if src not in self.resources or dst not in self.resources:
            return None
        if not self._passable(src, forbid) or not self._passable(dst, forbid):
            return None

        dist = {src: 0.0}
        prev: dict[str, Optional[str]] = {src: None}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, float("inf")):
                continue
            if u == dst:
                break
            for v in sorted(self.adj[u]):
                if not self._passable(v, forbid):
                    continue
                if capabilities is not None:
                    res = self.resources[v]
                    need = {t[4:] for t in res.tags if t.startswith("cap:")}
                    if need - capabilities:
                        continue
                nd = d + 1.0
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))

        if dst not in prev:
            return None
        path: list[str] = []
        cur: Optional[str] = dst
        while cur is not None:
            path.append(cur)
            cur = prev[cur]
        path.reverse()
        return path

    def fire_evacuation_path(self, src: str, zone: FireZone) -> Optional[list[str]]:
        """消防区内只允许朝指定出口撤离（不经过封闭资源）。"""
        return self.shortest_path(src, zone.exit_resource)

    def neighbors(self, rid: str) -> set[str]:
        return set(self.adj.get(rid, ()))
