"""责任归集：每个问题只计一次、去向唯一、争议挂起、重试不新增分配、对账后原子切换。"""
from __future__ import annotations

import copy

import pytest

from app import attribution, store

COLUMNS = ["row_id", "product_id", "category", "price", "supplier"]
RULES = {
    "missing_tokens": ["", "NA", "NULL", "-"],
    "supplier_column": "supplier",
    "business_key": ["product_id", "category"],
    "columns": {
        "row_id": {"rules": ["missing"]},
        "product_id": {"rules": ["missing"]},
        "category": {"rules": ["missing"]},
        "price": {"rules": ["missing"]},
        "supplier": {"rules": ["missing"]},
    },
}


def make_rows(suppliers: dict[str, str]) -> list[dict]:
    return [
        {"row_id": rid, "product_id": "P-" + rid, "category": "数码配件",
         "price": "100", "supplier": sup}
        for rid, sup in suppliers.items()
    ]


def empty_modules(rows: list[dict]):
    missing = {"total_rows": len(rows), "columns": []}
    duplicates = {
        "business_key": ["product_id", "category"],
        "duplicate_key": {"columns": ["product_id", "category"], "groups": [],
                          "group_count": 0, "row_count": 0, "extra_rows": 0, "row_ids": []},
        "duplicate_row": {"columns": [], "groups": [], "group_count": 0,
                          "row_count": 0, "extra_rows": 0, "row_ids": []},
        "excluded_from_key": 0,
    }
    return (missing, duplicates, {"cells": [], "insufficient": []},
            {"range": {"cells": [], "count": 0}, "type": {"cells": [], "count": 0}})


def dup_group(row_ids: list[str], kind: str = "duplicate_key") -> dict:
    return {"key": {"product_id": "P1", "category": "数码配件"},
            "count": len(row_ids), "extra": len(row_ids) - 1,
            "row_ids": row_ids, "label": "业务键 product_id=P1"}


def score_for(units: list[dict]) -> dict:
    """最小评分卡投影：每个问题一个单元，扣分按单元计。"""
    items = []
    for unit in units:
        items.append({
            "column": unit["column"],
            "rule": unit["rule"],
            "samples": 100,
            "violations": unit.get("violations", 1),
            "score": 1.0 - (unit.get("violations", 1) / 100),
            "deduction": unit.get("violations", 1) * 0.01,
        })
    total = round(100.0 - sum(i["deduction"] for i in items), 2)
    return {"items": items, "total": total, "deduction_total": round(100 - total, 6)}


def ledger_with(rows, dup_groups=(), outlier_cells=(), resolutions=None):
    missing, dup, out, val = empty_modules(rows)
    dup["duplicate_key"]["groups"] = list(dup_groups)
    out["cells"] = list(outlier_cells)
    ledger = attribution.build(rows, COLUMNS, RULES, missing, dup, out, val)
    ledger["resolutions"] = dict(resolutions or {})
    units = []
    for item in ledger["items"]:
        column = item["column"]
        rule = item["rules"][0]
        units.append({"column": column, "rule": rule, "violations": 1})
    ledger = attribution.attach_penalties(ledger, score_for(units))
    return attribution.finalize(ledger)


def test_cross_supplier_duplicate_group_is_one_pending_problem():
    rows = make_rows({"R1": "S01", "R2": "S02", "R3": "S03"})
    ledger = ledger_with(rows, dup_groups=[dup_group(["R1", "R2", "R3"])])
    assert ledger["item_count"] == 1           # 而不是 3 行 × 2 键列 = 6 条
    item = ledger["items"][0]
    assert item["status"] == "pending"
    assert item["owner"] is None
    assert item["supplier_candidates"] == ["S01", "S02", "S03"]
    assert item["deduction_share"] == 1
    assert len(item["original_rows"]) == 3      # 争议项保留原始行
    summary = ledger["summary"]
    assert summary["claimed"] == 0
    assert summary["pending"] == 1
    assert summary["deduction_pending"] == item["deduction"]


def test_reconcile_passes_for_clean_ledger_and_flags_tampering():
    rows = make_rows({"R1": "S01", "R2": "S01"})
    ledger = ledger_with(rows, dup_groups=[dup_group(["R1", "R2"])])
    assert attribution.reconcile(ledger)["ok"]

    tampered = copy.deepcopy(ledger)
    tampered["items"].append(dict(tampered["items"][0]))      # 同一问题入账两次
    result = attribution.reconcile(tampered)
    assert not result["ok"]
    names = [c["name"] for c in result["checks"] if not c["ok"]]
    assert "问题编号唯一" in names and "三桶互斥且合计等于问题总数" in names

    tampered = copy.deepcopy(ledger)
    tampered["items"][0].update({"status": "claimed", "owner": None})
    assert not attribution.reconcile(tampered)["ok"]


def test_manual_claim_moves_only_that_problem_and_is_idempotent():
    rows = make_rows({"R1": "S01", "R2": "S02"})
    ledger = ledger_with(rows, dup_groups=[dup_group(["R1", "R2"])])
    problem_id = ledger["items"][0]["problem_id"]
    resolutions = {problem_id: {"decision": "claim", "supplier": "S02", "note": "归属 S02"}}

    first = attribution.finalize(copy.deepcopy(ledger), resolutions)
    second = attribution.finalize(copy.deepcopy(ledger), resolutions)
    assert first["batch_key"] == second["batch_key"]
    item = first["items"][0]
    assert item["status"] == "claimed" and item["owner"] == "S02"
    assert item["source"] == "resolution"
    assert item["allocation_id"] == second["items"][0]["allocation_id"]
    assert first["summary"]["claimed"] == 1
    assert [s["supplier"] for s in first["summary"]["suppliers"]] == ["S02"]
    assert first["summary"]["deduction_claimed"] == item["deduction"]


def test_resolution_validation_rejects_bad_input():
    rows = make_rows({"R1": "S01", "R2": "S01"})
    ledger = ledger_with(rows, dup_groups=[dup_group(["R1", "R2"])])
    pid = ledger["items"][0]["problem_id"]
    with pytest.raises(attribution.AttributionError):
        attribution.finalize(copy.deepcopy(ledger),
                             {pid: {"decision": "claim", "supplier": ""}})
    with pytest.raises(attribution.AttributionError):
        attribution.finalize(copy.deepcopy(ledger), {"Q-NOPE": {"decision": "claim",
                                                                "supplier": "S01"}})
    with pytest.raises(attribution.AttributionError):
        attribution.finalize(copy.deepcopy(ledger), {pid: {"decision": "whatever"}})


def test_reprice_keeps_resolutions_and_does_not_add_problems():
    rows = make_rows({"R1": "S01", "R2": "S02"})
    ledger = ledger_with(rows, dup_groups=[dup_group(["R1", "R2"])])
    pid = ledger["items"][0]["problem_id"]
    ledger = attribution.finalize(copy.deepcopy(ledger),
                                  {pid: {"decision": "claim", "supplier": "S01"}})
    before_ids = [i["problem_id"] for i in ledger["items"]]

    repriced = attribution.reprice(ledger, score_for([
        {"column": ledger["items"][0]["column"],
         "rule": ledger["items"][0]["rules"][0], "violations": 5}
    ]))
    assert [i["problem_id"] for i in repriced["items"]] == before_ids
    item = repriced["items"][0]
    assert item["status"] == "claimed" and item["owner"] == "S01"
    assert item["deduction"] == pytest.approx(0.05)
    assert item["allocation_id"] == ledger["items"][0]["allocation_id"]
    assert attribution.reconcile(repriced)["ok"]


# ---------------------------------------------------------------- 存储与发布

RUN_ID = "test-attribution-run"


def make_publishable() -> dict:
    rows = make_rows({"R1": "S01", "R2": "S01", "R3": "S02", "R4": "S03"})
    missing, dup, out, val = empty_modules(rows)
    dup["duplicate_key"]["groups"] = [dup_group(["R1", "R2"]), dup_group(["R3", "R4"])]
    ledger = attribution.build(rows, COLUMNS, RULES, missing, dup, out, val)
    ledger["resolutions"] = {}
    units = [{"column": item["column"], "rule": item["rules"][0], "violations": 1}
             for item in ledger["items"]]
    ledger = attribution.attach_penalties(ledger, score_for(units))
    return attribution.finalize(ledger)


def fresh_run(run_id: str) -> None:
    store.connect()
    if store.get_run(run_id) is None:
        store.create_run(run_id, "unit-test", {})


def test_publish_is_atomic_and_retry_adds_no_allocations():
    fresh_run(RUN_ID)
    store.connect()
    ledger = make_publishable()
    reconcile = attribution.reconcile(ledger)
    assert reconcile["ok"]

    first = store.publish_attribution(RUN_ID, ledger, reconcile, {})
    assert first["state"] == "active"
    allocations = store.allocation_count(RUN_ID)
    assert allocations == ledger["summary"]["claimed"] > 0

    # 重试同一批：内容一致 → 不新增版本、不新增分配
    again = store.publish_attribution(RUN_ID, ledger, reconcile, {})
    assert again["version_id"] == first["version_id"]
    assert store.allocation_count(RUN_ID) == allocations

    # 仲裁后内容变化 → 新增版本并原子切换，旧版本保留为 superseded
    pid = next(i["problem_id"] for i in ledger["items"] if i["status"] == "pending")
    resolved = attribution.finalize(copy.deepcopy(ledger),
                                    {pid: {"decision": "claim", "supplier": "S03"}})
    reconcile2 = attribution.reconcile(resolved)
    second = store.publish_attribution(RUN_ID, resolved, reconcile2,
                                       {pid: {"decision": "claim", "supplier": "S03"}})
    assert second["version_id"] != first["version_id"]
    assert store.get_attribution(RUN_ID)["version_id"] == second["version_id"]
    assert store.get_attribution(RUN_ID, first["version_id"])["state"] == "superseded"
    assert store.allocation_count(RUN_ID) == ledger["summary"]["claimed"] + 1


def test_failed_publish_keeps_previous_active_version():
    run_id = RUN_ID + "-fail"
    fresh_run(run_id)
    ledger = make_publishable()
    reconcile = attribution.reconcile(ledger)
    active = store.publish_attribution(run_id, ledger, reconcile, {})
    before_alloc = store.allocation_count(run_id)

    broken = copy.deepcopy(ledger)
    broken["items"].append(dict(broken["items"][0]))
    bad_reconcile = attribution.reconcile(broken)
    assert not bad_reconcile["ok"]
    with pytest.raises(store.StoreError):
        store.publish_attribution(run_id, broken, bad_reconcile, {})

    still = store.get_attribution(run_id)
    assert still["version_id"] == active["version_id"]
    assert store.allocation_count(run_id) == before_alloc
    rejected = [v for v in store.list_attributions(run_id) if v["state"] == "rejected"]
    assert not rejected          # 直接调用 publish 失败时不写脏版本


def test_record_failed_attempt_does_not_disturb_active():
    run_id = RUN_ID + "-rejected"
    fresh_run(run_id)
    ledger = make_publishable()
    active = store.publish_attribution(run_id, ledger, attribution.reconcile(ledger), {})
    version_id = store.record_failed_attribution(run_id, ledger,
                                                 attribution.reconcile(ledger), {},
                                                 "模拟失败")
    assert version_id.endswith("-v2")
    assert store.get_attribution(run_id)["version_id"] == active["version_id"]
    states = {v["version_id"]: v["state"] for v in store.list_attributions(run_id)}
    assert states[version_id] == "rejected"


def test_two_runs_on_same_data_get_separate_allocations():
    """同一批数据跑两次不能互相吞掉分配流水（入库主键按 run 隔离）。"""
    ledger = make_publishable()
    reconcile = attribution.reconcile(ledger)
    run_a, run_b = RUN_ID + "-a", RUN_ID + "-b"
    fresh_run(run_a)
    fresh_run(run_b)
    store.publish_attribution(run_a, ledger, reconcile, {})
    store.publish_attribution(run_b, ledger, reconcile, {})
    expected = ledger["summary"]["claimed"]
    assert expected > 0
    assert store.allocation_count(run_a) == expected
    assert store.allocation_count(run_b) == expected
    ids_a = {a["allocation_id"] for a in store.allocations_for(run_a)}
    ids_b = {a["allocation_id"] for a in store.allocations_for(run_b)}
    assert ids_a.isdisjoint(ids_b)
    assert len(ids_a) == expected
