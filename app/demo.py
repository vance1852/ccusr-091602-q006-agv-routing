"""命令行演示：三车等待环从告警到解环、心跳丢失、人工接管的完整处置。

    python -m app.demo
"""

from __future__ import annotations

from app import scenarios
from app.models import LeaseState


def line(t=""):
    print(t)


def main() -> int:
    svc, clock = scenarios.build_service()
    line("=" * 68)
    line("无人工厂 · 路权协调服务值守演示")
    line("=" * 68)

    line("\n[1] 三台 AGV 各占一段窄巷并申请下一路段（并发请求）")
    scenarios.seed_three_way_cycle(svc, clock)
    g = svc.wait_graph()
    for e in g["edges"]:
        line(f"    {e['from']} 申请 {e['resource']} <- 被 {e['to']}"
             f"（{e['blocker_state']}）挡住")

    line("\n[2] 系统检测：")
    for inc in svc.incidents_view():
        if inc["status"] != "open":
            continue
        line(f"    [{inc['severity']}] {inc['kind']}: {inc['summary']}")
        for s in inc["suggested_actions"]:
            line(f"        建议动作: {s}")

    cycle = next(i for i in svc.incidents if i.kind == "wait_cycle")
    proposal = cycle.applied["proposal"]
    line("\n[3] 解环打分（危险级/电量/时限/退让可行性）：")
    for s in proposal["scores"]:
        if s["feasible"]:
            line(f"    {s['vehicle_id']}: 可行  score={s['score']} "
                 f"factors={s['factors']}")
        else:
            line(f"    {s['vehicle_id']}: 不可行 — {s['infeasible_reason']}")
    line(f"    => {proposal['rationale']}")

    line("\n[4] 执行退让（原路段在确认进入避让位前保持占用）：")
    out = svc.apply_resolution(cycle.incident_id)
    held_before = [l.resource_id for l in svc.ledger.vehicle_leases(
        proposal["target"], frozenset({LeaseState.ENTERED}))]
    line(f"    已发退让租约 {out['lease']['lease_id']} -> "
         f"{out['lease']['resource_id']}；{proposal['target']} 仍占用 {held_before}")
    svc.enter_resource(proposal["target"], out["lease"]["lease_id"])
    held_after = [l.resource_id for l in svc.ledger.vehicle_leases(
        proposal["target"], frozenset({LeaseState.ENTERED, LeaseState.UNCERTAIN}))]
    line(f"    车辆确认进入避让位后，实际占用 = {held_after}（原窄巷已释放）")

    line("\n[5] 心跳丢失演练（仅让 V3 静默超时）：")
    clock.advance(20)
    # V1/V2 在超时窗口内正常上报，只有 V3 静默
    svc.heartbeat("V1", "A1")
    svc.heartbeat("V2", "C2")
    tick = svc.tick()
    line(f"    tick: {tick}")
    for lid in tick["heartbeat_lost"]:
        l = svc.ledger.get(lid)
        line(f"    租约 {lid}（{l.vehicle_id}/{l.resource_id}）状态 = "
             f"{l.state.value}（不释放，等待位置确认）")
    lost_v3 = next(lid for lid in tick["heartbeat_lost"]
                   if svc.ledger.get(lid).vehicle_id == "V3")
    line("    值守员核实后确认 V3 仍在 A3 =>")
    svc.confirm_position("V3", "A3", occupied=True)
    line(f"    V3 租约恢复 = {svc.ledger.get(lost_v3).state.value}")

    line("\n[6] 人工接管永远优先：")
    r = svc.manual_takeover("V2", reason="值守员远程介入")
    line(f"    {r}")
    d = svc.request_resource("V2", "A3", request_id="manual-demo")
    line(f"    接管期间申请被拒: {d['status']} / {d['reason']}")

    line("\n[7] 全程不变量校验：")
    line(f"    台账冲突 = {svc.integrity_violations()}")
    from app.replay import assert_no_conflicting_grants
    line(f"    事件流授权重叠 = {assert_no_conflicting_grants(svc.events.all())}")
    line(f"    事件总数 = {len(svc.events.all())}（可通过 /api/events 审计回放）")
    line("\n完成。启动 API：python -m app.api --demo --port 8080")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
