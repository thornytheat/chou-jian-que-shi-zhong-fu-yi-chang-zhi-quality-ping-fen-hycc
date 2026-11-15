"""剖析流水线：把业务模块串成一次可复现的运行。

顺序固定（缺失 → 重复 → 异常 → 类型/范围 → 评分 → 责任归集 → 告警），每一步都写入
阶段耗时，便于结果页展示「依据来自哪一步」。纯函数，无 IO，可直接单测。

责任归集阶段（``attribution``）先于告警：账本是唯一事实来源，告警只是账本的投影。
同一问题在账本中只有一条，重复组按「组」计一次，跨供应商重复不会按行重复处罚。
"""
from __future__ import annotations

import time

from . import alerts, attribution, duplicates, missing, outliers, score, validators
from .loader import missing_tokens

STAGES = [
    ("load", "载入数据与规则"),
    ("missing", "缺失率检查"),
    ("duplicates", "重复检测"),
    ("outliers", "异常值检测（IQR + 稳健z）"),
    ("validation", "类型与范围校验"),
    ("score", "加权评分"),
    ("attribution", "责任归集与对账"),
    ("alerts", "告警合并去重"),
]


def run(rows: list[dict[str, str]], columns: list[str], rules: dict,
        weights: dict | None = None, progress=None,
        resolutions: dict | None = None) -> dict:
    stages: list[dict] = []
    started = time.perf_counter()

    def step(index: int) -> None:
        name, label = STAGES[index]
        stages.append({"name": name, "label": label, "ms": 0.0})
        if progress:
            progress(index, label)

    def close(index: int) -> None:
        stages[index]["ms"] = round((time.perf_counter() - started) * 1000, 2)

    step(0)
    tokens = missing_tokens(rules)
    close(0)

    step(1)
    missing_result = missing.analyze(rows, columns, rules)
    close(1)

    step(2)
    dup_result = duplicates.analyze(rows, columns, rules)
    close(2)

    step(3)
    outlier_result = outliers.analyze(rows, columns, rules)
    close(3)

    step(4)
    validation = validators.analyze(rows, columns, rules, tokens)
    close(4)

    step(5)
    units = score.build_units(missing_result, dup_result, outlier_result, validation, rules)
    scored = score.compute(units, rules, weights)
    close(5)

    step(6)
    ledger = attribution.build(rows, columns, rules, missing_result, dup_result,
                               outlier_result, validation)
    ledger = attribution.attach_penalties(ledger, scored)
    ledger["resolutions"] = dict(resolutions or {})
    ledger = attribution.finalize(ledger)
    reconcile = attribution.reconcile(ledger)
    close(6)

    step(7)
    alert_result = alerts.build(ledger)
    close(7)

    report = {
        "dataset": {
            "rows": len(rows),
            "columns": len(columns),
            "column_names": columns,
            "rule_version": rules.get("version", "1"),
            "business_key": dup_result["business_key"],
            "group_by": rules.get("group_by", "category"),
        },
        "missing": missing_result,
        "duplicates": dup_result,
        "outliers": outlier_result,
        "validation": validation,
        "score": scored,
        "alerts": alert_result,
        "attribution": ledger,
        "reconcile": reconcile,
        "resolutions": dict(resolutions or {}),
        "stages": stages,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "rules_snapshot": _rules_snapshot(rules),
    }
    return report


def _rules_snapshot(rules: dict) -> dict:
    return {
        "version": rules.get("version"),
        "business_key": rules.get("business_key"),
        "group_by": rules.get("group_by"),
        "min_group_sample": rules.get("min_group_sample"),
        "outlier": rules.get("outlier"),
        "grade_thresholds": rules.get("grade_thresholds"),
        "rule_weights": rules.get("rule_weights"),
        "columns": {
            name: {
                "type": spec.get("type"),
                "max_missing_rate": spec.get("max_missing_rate"),
                "weight": spec.get("weight"),
                "rules": spec.get("rules"),
                "range": spec.get("range"),
            }
            for name, spec in rules["columns"].items()
        },
    }
