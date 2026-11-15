"""告警与工单视图：把责任归集账本投影成页面/CSV 可直接消费的告警列表。

口径与 ``attribution`` 完全一致：**一个账本条目 = 一条告警**。
同一 ``(row_id, column)`` 的多条规则在账本阶段已经合并，重复组也只按「组」计一条，
因此这里不再做二次展开——避免「按行汇总是重复处罚」的老问题。
"""
from __future__ import annotations

from .attribution import SEVERITY_LABEL, STATUS_LABEL, severity_rank


def build(ledger: dict) -> dict:
    items = []
    for entry in ledger.get("items") or []:
        items.append({
            "problem_id": entry["problem_id"],
            "row_id": entry["row_id"],
            "column": entry["column"],
            "column_label": entry["column"],
            "rules": list(entry["rules"]),
            "rule_labels": list(entry.get("rule_labels") or []),
            "rules_text": entry["rules_text"],
            "merged_from": entry["merged_from"],
            "severity": entry["severity"],
            "severity_label": SEVERITY_LABEL.get(entry["severity"], entry["severity"]),
            "value": entry.get("value") or "—",
            "reason": entry.get("reason") or "",
            "suggestion": entry.get("suggestion") or "",
            "scope": entry.get("scope") or "cell",
            "group_rows": entry.get("group_rows"),
            "rows_affected": entry.get("rows_affected"),
            "status": entry["status"],
            "status_label": STATUS_LABEL.get(entry["status"], entry["status"]),
            "owner": entry.get("owner"),
            "supplier_candidates": list(entry.get("supplier_candidates") or []),
            "source": entry.get("source"),
            "resolution_note": entry.get("resolution_note") or "",
            "deduction": entry.get("deduction") or 0.0,
            "kind": entry.get("kind"),
        })
    items.sort(key=lambda a: (-severity_rank(a["severity"]), a["column"], a["row_id"]))
    return finalize(items)


def finalize(items: list[dict]) -> dict:
    by_rule: dict[str, int] = {}
    for item in items:
        for rule in item["rules"]:
            by_rule[rule] = by_rule.get(rule, 0) + 1
    return {
        "items": items,
        "total": len(items),
        "high": sum(1 for i in items if i["severity"] == "high"),
        "medium": sum(1 for i in items if i["severity"] == "medium"),
        "low": sum(1 for i in items if i["severity"] == "low"),
        "merged_cells": sum(1 for i in items if i["merged_from"] > 1),
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "by_severity": {
            "high": sum(1 for i in items if i["severity"] == "high"),
            "medium": sum(1 for i in items if i["severity"] == "medium"),
            "low": sum(1 for i in items if i["severity"] == "low"),
        },
        "by_status": {
            status: sum(1 for i in items if i["status"] == status)
            for status in ("claimed", "pending", "unassigned")
        },
    }
