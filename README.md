# AGV 路权防死锁调度

无人工厂窄巷/路口/消防通道的**独立路权协调服务**。车辆每次只能凭有效租约
（lease）进入下一资源；服务负责时窗预约、等待环检测与解环、安全门控、
消防撤离、重规划与事件回放。仅依赖 Python 标准库，可独立运行。

## 核心安全语义

- **时间窗左闭右开** `[start, end)`：首尾相接的预约不冲突，任何重叠即冲突。
- **租约状态机**（与 `domain_contract.json` 一致）：
  `requested → granted → entered → released`；
  - 心跳丢失/故障：`entered → uncertain`，**绝不直接释放**，
    必须先经位置确认（仍占用则恢复 `entered`，确认清空才 `released`）；
  - 只有从未进入的 `granted` 授权可 `expired` 回收。
- **安全永远优先**：安全急停、人工接管期间停止一切新授权，已占用租约保留。
- **消防区不可降级**：已进入消防区的车辆只能沿拓扑指定出口的路径撤离，
  申请逆行/改道一律拒绝。
- **不抢占已发租约**：单纯提高优先级不会抢资源（避免堵塞消防通道）；
  优先级反转只告警，死锁由解环策略处理。

## 解环策略

等待图边 `A → B` 表示 A 申请的资源被 B 持有。检测到环后对环上每台车按
**载荷危险级、剩余电量（须付得起退让能耗并保留安全余量）、任务时限余量、
退让可行性**（身后有空闲资源/避让位、非消防区、非离线/故障/安全模式）打分，
选择唯一可行且代价最小的车退入避让位；原占用路段在车辆确认进入避让位后才释放。
无可退让车辆时给出 `human_assist`；环含消防区车辆时给出指定出口撤离动作。

## 重规划

路段封闭或车辆故障时，**只重新规划尚未通过的路段**：以车辆当前确认位置为起点、
绕开封闭与 `uncertain` 资源做受限 Dijkstra；原路线标记 `superseded` 归档，
保留父计划与完整决策理由（`GET /api/plans/<vid>`）。

## 运行

```bash
python3 -m unittest discover -s tests -v   # 30 个测试
python3 -m app.demo                        # 值守处置流程演示
python3 -m app.api --demo --port 8080      # HTTP API + 预置三车等待环
```

## 值守员 API

| 方法/路径 | 用途 |
| --- | --- |
| `GET /api/status` | 总览：等待图、车辆状态、开放告警、不变量校验 |
| `GET /api/wait-graph` | 等待图（节点/边/阻塞租约状态） |
| `GET /api/timeline?resource=A2` | 预约时间轴 |
| `GET /api/denials` | 被拒/排队原因（占用、封闭、能力、消防逆行、急停/接管…） |
| `GET /api/incidents` | 告警、评分与建议动作 |
| `POST /api/incidents/<id>/apply` | 执行建议动作（retreat / evacuate_fire_zone / human_assist） |
| `POST /api/leases/request` | 申请资源（同一 `request_id` 重试返回原预约） |
| `POST /api/leases/enter` | 凭有效租约进入 |
| `POST /api/heartbeat` | 心跳+自报位置 |
| `POST /api/position-confirm` | 人工/定位确认后处置 uncertain 租约 |
| `POST /api/safe-stop` / `manual-takeover` / `resume` | 安全控制 |
| `POST /api/segments/close` | 封闭路段并触发仅未通过路段的重规划 |
| `GET /api/events` | 只追加事件流（审计与回放） |

## 模块

```
app/
  clock.py     仿真/系统时钟
  models.py    资源、车辆、租约、路线、事件（契约字面值）
  topology.py  有向拓扑、消防区、封闭、受限寻路
  ledger.py    时间窗台账与租约状态机（冲突检测/完整性扫描）
  fleet.py     车辆运行状态、事件存储
  policy.py    等待图、环检测、解环评分
  service.py   协调服务（授权门控、安全、重规划、处置、态势查询）
  replay.py    命令录制、确定性回放、授权重叠审计
  scenarios.py 示例拓扑（三车窄巷环 + 消防区）
  api.py       标准库 HTTP API（串行化授权）
  demo.py      命令行值守演示
```

`Recorder.replay()` 在全新实例上确定性重放全部命令，并在**每一步之后**扫描台账：
任何两车在同一资源上持有时间窗重叠的有效租约都会立即判定失败。
