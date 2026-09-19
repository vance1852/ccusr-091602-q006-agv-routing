"""仓库拓扑：路段与路口资源、消防区与指定出口、最短路规划。

拓扑是独立事实：路线计划不代表物理占用，车辆位置以确认回报为准。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class Resource:
    resource_id: str
    kind: str = "segment"          # segment | intersection
    capacity: int = 1              # 窄巷/路口默认容量 1
    closed: bool = False


@dataclass
class Segment(Resource):
    start_node: str = ""
    end_node: str = ""
    length: float = 1.0


@dataclass
class FireZone:
    """消防区：区内车辆只能沿指定出口路段撤离。"""

    zone_id: str
    segments: frozenset[str]
    exits: tuple[str, ...]


class Topology:
    def __init__(self) -> None:
        self.resources: dict[str, Resource] = {}
        self.fire_zones: dict[str, FireZone] = {}

    # ---- 构建 ----

    def add_segment(
        self,
        segment_id: str,
        start_node: str,
        end_node: str,
        length: float = 1.0,
        capacity: int = 1,
    ) -> Segment:
        seg = Segment(
            resource_id=segment_id,
            kind="segment",
            capacity=capacity,
            start_node=start_node,
            end_node=end_node,
            length=length,
        )
        self.resources[segment_id] = seg
        return seg

    def add_intersection(self, resource_id: str, capacity: int = 1) -> Resource:
        res = Resource(resource_id=resource_id, kind="intersection", capacity=capacity)
        self.resources[resource_id] = res
        return res

    def add_fire_zone(
        self, zone_id: str, segments: list[str], exits: list[str]
    ) -> FireZone:
        zone = FireZone(zone_id=zone_id, segments=frozenset(segments), exits=tuple(exits))
        self.fire_zones[zone_id] = zone
        return zone

    # ---- 查询 ----

    def get(self, resource_id: str) -> Resource | None:
        return self.resources.get(resource_id)

    def segment(self, resource_id: str) -> Segment | None:
        res = self.resources.get(resource_id)
        return res if isinstance(res, Segment) else None

    def is_closed(self, resource_id: str) -> bool:
        res = self.resources.get(resource_id)
        return bool(res and res.closed)

    def close(self, resource_id: str) -> None:
        self.resources[resource_id].closed = True

    def open(self, resource_id: str) -> None:
        self.resources[resource_id].closed = False

    def fire_zone_of(self, segment_id: str) -> FireZone | None:
        for zone in self.fire_zones.values():
            if segment_id in zone.segments:
                return zone
        return None

    def is_fire_exit(self, segment_id: str) -> bool:
        """是否为某消防区的指定出口（区外车辆不得把它当退让/停靠点）。"""
        return any(segment_id in zone.exits for zone in self.fire_zones.values())

    def successors(self, node: str, avoid: frozenset[str] = frozenset()) -> list[Segment]:
        """从节点出发、未封闭且未被回避的路段。"""
        out = []
        for res in self.resources.values():
            if (
                isinstance(res, Segment)
                and res.start_node == node
                and not res.closed
                and res.resource_id not in avoid
            ):
                out.append(res)
        return out

    def shortest_path(
        self, start_node: str, goal_node: str, avoid: frozenset[str] = frozenset()
    ) -> list[str] | None:
        """BFS 最短路，返回路段 id 序列；不可达返回 None。"""
        if start_node == goal_node:
            return []
        visited = {start_node}
        queue: deque[tuple[str, list[str]]] = deque([(start_node, [])])
        while queue:
            node, path = queue.popleft()
            for seg in self.successors(node, avoid):
                if seg.end_node in visited:
                    continue
                new_path = path + [seg.resource_id]
                if seg.end_node == goal_node:
                    return new_path
                visited.add(seg.end_node)
                queue.append((seg.end_node, new_path))
        return None
