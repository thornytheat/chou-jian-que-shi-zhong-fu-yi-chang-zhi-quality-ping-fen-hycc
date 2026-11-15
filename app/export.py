"""工单导出：把责任归集后的告警渲染成可直接下发的 CSV（Excel 友好，带 BOM）。

每行 = 账本中的一个问题（一个问题只出现一次）；已认领带供应商，待仲裁列出候选，
未分配明确标注，派单前先看「去向」列。
"""
from __future__ import annotations

import csv
import io

HEADER = [
    "工单号", "问题编号", "row_id", "column", "rules", "severity",
    "去向", "供应商", "候选供应商", "本次扣分", "当前值", "问题说明", "整改建议", "仲裁备注",
]


def alerts_to_csv(alerts: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(HEADER)
    for index, item in enumerate(alerts["items"], start=1):
        writer.writerow([
            f"WO-{index:04d}",
            item.get("problem_id", ""),
            item["row_id"],
            item["column"],
            item["rules_text"],
            item["severity"],
            item.get("status_label", ""),
            item.get("owner") or "",
            "、".join(item.get("supplier_candidates") or []),
            f"{float(item.get('deduction') or 0.0):.4f}",
            item["value"],
            item["reason"],
            item["suggestion"],
            item.get("resolution_note") or "",
        ])
    return "﻿" + buffer.getvalue()
