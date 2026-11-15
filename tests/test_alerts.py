"""告警视图：账本中的每个问题投影成一条告警，重复组不按行重复展开。

这些测试通过 ``attribution.build`` 走真实账本，再交给 ``alerts.build``，
确保「页面/工单看到的一条 = 总分中计入的一次」。
"""
from __future__ import annotations

from app import attribution

COLUMNS = ["row_id", "product_id", "category", "price", "supplier"]
RULES = {
    "missing_tokens": ["", "NA", "NULL", "-"],
    "supplier_column": "supplier",
    "business_key": ["product_id", "category"],
    "columns": {
        "price": {"rules": ["missing"]},
        "product_id": {"rules": ["missing"]},
        "category": {"rules": ["missing"]},
        "supplier": {"rules": ["missing"]},
        "row_id": {"rules": ["missing"]},
    },
}


def make_rows(suppliers: dict[str, str]) -> list[dict]:
    rows = []
    for rid, sup in suppliers.items():
        rows.append({
            "row_id": rid,
            "product_id": "P-" + rid,
            "category": "数码配件",
            "price": "100",
            "supplier": sup,
        })
    return rows


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
    outliers = {"cells": [], "insufficient": []}
    validation = {"range": {"cells": [], "count": 0}, "type": {"cells": [], "count": 0}}
    return missing, duplicates, outliers, validation


def missing_item(column: str, missing: int, status: str = "violation",
                 rate: float = 0.2, max_rate: float = 0.05) -> dict:
    return {
        "column": column, "total": 100, "missing": missing, "rate": rate,
        "max_rate": max_rate, "status": status,
        "note": f"缺失 {missing}/100 超过允许 {max_rate:.0%}",
    }


def outlier_cell(row_id: str, column: str = "price", severity: str = "high",
                 iqr: bool = True, mad: bool = True) -> dict:
    return {
        "row_id": row_id, "column": column, "group": "数码配件", "value": 99999.0,
        "severity": severity, "iqr_hit": iqr, "mad_hit": mad, "robust_z": 42.0,
        "mad_zero": False, "bounds": [100.0, 500.0], "median": 300.0, "mad": 50.0,
        "methods": [],
    }


def build_ledger(rows, missing, duplicates, outliers, validation, resolutions=None):
    ledger = attribution.build(rows, COLUMNS, RULES, missing, duplicates,
                               outliers, validation)
    ledger["resolutions"] = dict(resolutions or {})
    return attribution.finalize(ledger)


def test_same_cell_merged_into_one_alert():
    rows = make_rows({"R1": "S01"})
    missing, dup, out, val = empty_modules(rows)
    out["cells"] = [outlier_cell("R1")]
    val["range"]["cells"] = [{
        "row_id": "R1", "column": "price", "value": 99999.0, "raw": "99999.00",
        "range": [0.5, 50000], "reason": "取值 99999 超出允许范围 [0.5, 50000]",
    }]
    ledger = build_ledger(rows, missing, dup, out, val)
    alert = ledger["items"][0]
    assert ledger["item_count"] == 1
    assert alert["row_id"] == "R1" and alert["column"] == "price"
    assert alert["rules"] == ["range", "outlier"]
    assert alert["merged_from"] == 2
    assert alert["severity"] == "high"
    assert "超出允许范围" in alert["reason"] and "稳健 z" in alert["reason"]
    assert alert["suggestion"]
    assert alert["status"] == "claimed" and alert["owner"] == "S01"


def test_different_cells_are_not_merged():
    rows = make_rows({"R1": "S01", "R2": "S01"})
    missing, dup, out, val = empty_modules(rows)
    out["cells"] = [outlier_cell("R1"), outlier_cell("R2"), outlier_cell("R1", "product_id")]
    ledger = build_ledger(rows, missing, dup, out, val)
    keys = {(i["row_id"], i["column"]) for i in ledger["items"]}
    assert len(keys) == 3
    assert ledger["item_count"] == 3


def test_severity_takes_max_of_mixed_rules():
    rows = make_rows({"R9": "S09"})
    rows[0]["price"] = ""
    missing, dup, out, val = empty_modules(rows)
    missing["columns"] = [missing_item("price", 1)]
    out["cells"] = [outlier_cell("R9", severity="medium", iqr=True, mad=False)]
    ledger = build_ledger(rows, missing, dup, out, val)
    assert ledger["item_count"] == 1
    item = ledger["items"][0]
    assert set(item["rules"]) == {"missing", "outlier"}
    assert item["severity"] == "medium"


def test_all_empty_column_produces_single_column_alert():
    rows = make_rows({f"R{i}": "S01" for i in range(1, 5)})
    for row in rows:
        row["price"] = ""
    missing, dup, out, val = empty_modules(rows)
    missing["columns"] = [missing_item("price", 4, status="all_empty", rate=1.0,
                                       max_rate=0.02)]
    ledger = build_ledger(rows, missing, dup, out, val)
    assert ledger["item_count"] == 1
    item = ledger["items"][0]
    assert item["row_id"] == "—"
    assert item["column"] == "price"
    assert item["scope"] == "column"
    assert item["severity"] == "high"
    assert item["rows_affected"] == 4
    # 整列全空保留全部受影响原始行，且只有一条问题
    assert len(item["original_rows"]) == 4


def test_missing_violation_creates_one_problem_per_cell():
    rows = make_rows({f"R{i:03d}": "S01" for i in range(1, 101)})
    for row in rows:
        if row["row_id"] in {f"R{i:03d}" for i in range(1, 101)}:
            row["price"] = ""
    missing, dup, out, val = empty_modules(rows)
    missing["columns"] = [missing_item("price", 100, rate=1.0, max_rate=0.02)]
    ledger = build_ledger(rows, missing, dup, out, val)
    # 不再用「前 20 行 + 列汇总」，而是 100 个真实问题，每个只计一次
    assert ledger["item_count"] == 100
    assert sum(1 for i in ledger["items"] if i["scope"] == "cell") == 100


def test_duplicate_key_group_is_one_problem_not_rows_times_columns():
    rows = make_rows({"R1": "S01", "R2": "S02", "R3": "S03"})
    missing, dup, out, val = empty_modules(rows)
    dup["duplicate_key"]["groups"] = [{
        "key": {"product_id": "P1", "category": "数码配件"},
        "count": 3, "extra": 2, "row_ids": ["R1", "R2", "R3"],
        "label": "业务键 product_id=P1 + category=数码配件",
    }]
    ledger = build_ledger(rows, missing, dup, out, val)
    assert ledger["item_count"] == 1
    item = ledger["items"][0]
    assert item["kind"] == "duplicate_key"
    assert item["column"] == attribution.DUP_KEY_UNIT
    assert item["group_rows"] == ["R1", "R2", "R3"]
    assert len(item["original_rows"]) == 3
    # 跨供应商 → 待仲裁，不落到任何一方
    assert item["status"] == "pending"
    assert item["owner"] is None
    assert item["supplier_candidates"] == ["S01", "S02", "S03"]


def test_duplicate_row_group_scope_and_owner():
    rows = make_rows({"R1": "S01", "R2": "S01"})
    missing, dup, out, val = empty_modules(rows)
    dup["duplicate_row"]["groups"] = [{
        "key": {"product_id": "P1"}, "count": 2, "extra": 1,
        "row_ids": ["R1", "R2"], "label": "整行",
    }]
    ledger = build_ledger(rows, missing, dup, out, val)
    assert ledger["item_count"] == 1
    item = ledger["items"][0]
    assert item["scope"] == "row"
    assert item["column"] == attribution.ROW_UNIT
    assert item["severity"] == "high"
    assert item["status"] == "claimed" and item["owner"] == "S01"


def test_shared_problem_claimed_by_one_supplier_does_not_charge_others():
    rows = make_rows({"R1": "S01", "R2": "S02"})
    missing, dup, out, val = empty_modules(rows)
    dup["duplicate_key"]["groups"] = [{
        "key": {"product_id": "P1", "category": "数码配件"},
        "count": 2, "extra": 1, "row_ids": ["R1", "R2"], "label": "业务键",
    }]
    ledger = build_ledger(rows, missing, dup, out, val)
    item = ledger["items"][0]
    pid = item["problem_id"]
    ledger = build_ledger(rows, missing, dup, out, val, resolutions={
        pid: {"decision": "claim", "supplier": "S02", "note": "主数据归属 S02"}
    })
    assert ledger["item_count"] == 1
    assert ledger["items"][0]["status"] == "claimed"
    assert ledger["items"][0]["owner"] == "S02"
    summary = ledger["summary"]
    assert [s["supplier"] for s in summary["suppliers"]] == ["S02"]
    assert summary["suppliers"][0]["problems"] == 1


def test_by_rule_counts_and_severity_order():
    rows = make_rows({"R1": "S01", "R2": "S02"})
    rows[0]["category"] = ""
    missing, dup, out, val = empty_modules(rows)
    missing["columns"] = [missing_item("category", 1)]
    out["cells"] = [outlier_cell("R1")]
    val["type"]["cells"] = [{
        "row_id": "R2", "column": "price", "value": "面议",
        "expected": "数字", "reason": "「面议」不是合法数字",
    }]
    ledger = build_ledger(rows, missing, dup, out, val)
    from app import alerts as alert_view
    result = alert_view.build(ledger)
    assert result["by_rule"]["outlier"] == 1
    assert result["by_rule"]["missing"] == 1
    assert result["by_rule"]["type"] == 1
    assert result["items"][0]["severity"] == "high"
    assert result["total"] == len(ledger["items"])


def test_sample_data_alerts_merged_once_per_cell(dataset, rules):
    from app import duplicates, missing, outliers, validators
    from app.loader import missing_tokens
    from app import alerts as alert_view

    tokens = missing_tokens(rules)
    miss = missing.analyze(dataset["rows"], dataset["columns"], rules)
    dup = duplicates.analyze(dataset["rows"], dataset["columns"], rules)
    out = outliers.analyze(dataset["rows"], dataset["columns"], rules)
    val = validators.analyze(dataset["rows"], dataset["columns"], rules, tokens)
    ledger = build_ledger_sample(dataset, rules, miss, dup, out, val)
    result = alert_view.build(ledger)

    keys = [(i["row_id"], i["column"]) for i in result["items"]]
    assert len(keys) == len(set(keys))            # 同一单元格只出现一次
    assert result["total"] == ledger["item_count"]
    merged = [i for i in result["items"] if i["merged_from"] > 1]
    assert merged, "样例里应有同格多规则命中的单元格"
    target = next(i for i in result["items"]
                  if i["row_id"] == "R000031" and i["column"] == "price")
    assert set(target["rules"]) == {"range", "outlier"}
    assert target["severity"] == "high"


def build_ledger_sample(dataset, rules, miss, dup, out, val):
    ledger = attribution.build(dataset["rows"], dataset["columns"], rules,
                               miss, dup, out, val)
    return attribution.finalize(ledger)
