# AGV 路权防死锁调度

本项目协调自动导引车对窄巷、路口和安全通道的使用。地图拓扑与车辆实时位置是独立事实，路线计划不能直接代表物理占用。

`domain_contract.json` 规定租约、车辆控制权和安全事件的外部状态。所有预约区间采用左闭右开语义，安全急停与人工接管具有不可降低的优先级。

## 架构

```
app/
  clock.py        时钟抽象（SystemClock / ManualClock，回放与测试用）
  topology.py     路段/路口资源、消防区与指定出口、BFS 最短路
  models.py       租约、车辆、任务模型（状态取值与 domain_contract.json 对齐）
  events.py       事件日志：command（外部输入，可回放）/ derived（派生结论）
  coordinator.py  核心：时窗预约、租约状态机、超时处理、局部重规划、不变量校验
  deadlock.py     等待图、环检测、解环动作评分与选择、优先级继承
  service.py      标准库 HTTP 服务（车辆侧 + 值守员侧 API）
  demo.py         三车窄巷互等场景：解环、抢占、心跳丢失、人工接管、回放
```

### 租约状态机

```
requested --授予--> granted --进入--> entered --离开--> released
requested/granted/entered --心跳或窗口超时--> uncertain
uncertain --位置确认:仍在资源上--> 恢复原状态
uncertain --位置确认:不在资源上--> expired（资源才释放）
granted 窗口过期且从未进入 --> expired（资源从未被物理占用，可安全终结）
```

- **幂等预约**：`request_id` 去重，通信重试返回原预约（或原拒绝），不产生新租约。
- **超时不释放**：租约超时一律先转 `uncertain` 等待位置确认；只有确认车辆
  不在资源上才允许 `expired`。仍被占用的路段绝不直接释放。
- **抢占**：仅允许抢占 `granted`（未进入）且持有者为 automatic 的租约，
  且申请方优先级须高出阈值；`entered` 租约与急停/接管车辆不可抢占。

### 死锁处理

等待图边 `w -> h` 表示 w 的申请被 h 持有的活跃租约挡住；环即死锁。
解环动作按代价选择（危险级×10 + 缺电×5 + 时限紧迫度×20），硬约束：

- `safe_stop` / `manual` 车辆永远不被自动挪动（安全急停与人工接管优先）；
- 消防区内车辆只能沿指定出口撤离；
- 退让目标不得占用任何消防区指定出口（避免堵住消防通道）；
- 无可行动作时输出 `manual` 建议，交由值守员处理。

优先级反转通过**优先级继承**缓解：被高优先级等待者阻塞的持有者临时
继承其优先级，影响等待申请的重试顺序。

### 局部重规划

路段封闭或车辆故障后，仅重规划**尚未通过**的路段（`task.passed` 前缀
不可改写），原路线尾段、重规划原因与决策理由全部保留在
`task.revisions` / `task.rationale` 中。故障车所在段标记为动态阻塞，
其租约转 `uncertain` 等待位置确认，绝不直接释放。

### 回放与不变量

一切状态变更先落事件日志。`POST /operator/replay` 按 command 事件重建
状态机并逐步校验安全不变量：**任何资源在任何时刻的并发持有数不得超过
容量**（预约窗口维度 + 物理占用维度），即不会出现两车同时获准进入冲突
资源。

## 运行

```bash
python3 -m unittest discover -s tests -v   # 测试
python3 -m app.demo                        # 演示场景（含回放校验）
python3 -m app --port 8080 [--demo]        # 独立运行服务
```

## API 摘要

车辆侧：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/topology/segments` `/intersections` `/fire-zones` | 构建拓扑 |
| POST | `/topology/segments/{id}/close` `/open` | 路段封闭/恢复 |
| POST | `/vehicles` | 注册车辆（能力、危险级、电量） |
| POST | `/vehicles/{id}/task` | 下达任务（路线、时限） |
| POST | `/reservations` | 时窗预约（`request_id` 幂等） |
| POST | `/leases/{id}/enter` `/release` | 凭有效租约进入/释放 |
| POST | `/vehicles/{id}/heartbeat` `/position` | 心跳 / 位置确认 |
| POST | `/vehicles/{id}/control` `/fault` | 控制模式切换 / 故障上报 |

值守员侧（告警界面）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/operator/wait-graph` | 等待图（节点、边、环） |
| GET | `/operator/timeline?resource_id=` | 预约时间轴 |
| GET | `/operator/rejections` | 被拒绝原因 |
| GET | `/operator/suggestions` | 建议动作（解环、位置确认、优先级继承） |
| GET | `/operator/leases` `/vehicles` `/events` | 租约 / 车辆 / 事件日志 |
| GET | `/operator/invariants` | 当前不变量校验 |
| POST | `/operator/replay` | 回放全部事件并校验不变量 |
