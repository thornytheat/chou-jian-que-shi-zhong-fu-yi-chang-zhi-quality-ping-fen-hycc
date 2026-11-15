"""责任归集：把一次抽检中的问题整理成「唯一账本」，并按供应商给出明确去向。

三个口径（与评分卡、告警、工单解耦，便于单测）：

1. **每个问题只算一次**：单元格问题按 ``(row_id, column)`` 合并所有命中规则；
   重复组按「组」计一条，不再按「行 × 键列」展开。跨供应商重复组不会再对每一行、
   每一个供应商各扣一次分。
2. **三类去向**：``claimed``（已认领）/ ``pending``（待仲裁）/ ``unassigned``（未分配）。
   供应商候选来自问题涉及行的 ``supplier`` 列：0 个候选 → 未分配；1 个候选 → 已认领；
   多个候选 → 待仲裁。争议项挂起，原样保留每一行的原始数据。
3. **对账**：``reconcile`` 校验编号唯一、三桶互斥且合计等于问题总数、负责人与状态一致、
   供应商结果不重复计入、重复组只计一次、问题扣分合计等于总分扣减。只有对账通过
   的结果才允许被 ``store`` 原子切换为生效版本。

``apply_resolutions`` 在自动判定（base_status/base_owner）之上套用人工仲裁结论；
同一批输入 + 同一批仲裁结论得到同一个 ``batch_key``，重试不会新增分配。
"""
from __future__ import annotations

import copy
import hashlib
import json

from .loader import missing_tokens

STATUS_CLAIMED = "claimed"
STATUS_PENDING = "pending"
STATUS_UNASSIGNED = "unassigned"
STATUSES = (STATUS_CLAIMED, STATUS_PENDING, STATUS_UNASSIGNED)
STATUS_LABEL = {
    STATUS_CLAIMED: "已认领",
    STATUS_PENDING: "待仲裁",
    STATUS_UNASSIGNED: "未分配",
}
DECISION_CLAIM = "claim"
DECISION_PENDING = "pending"
DECISION_UNASSIGN = "unassign"
DECISIONS = (DECISION_CLAIM, DECISION_PENDING, DECISION_UNASSIGN)

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
SEVERITY_LABEL = {"low": "低", "medium": "中", "high": "高"}

RULE_LABEL = {
    "missing": "缺失",
    "type": "类型错误",
    "range": "越界",
    "outlier": "异常值",
    "duplicate_key": "复合键重复",
    "duplicate_row": "整行重复",
}
RULE_ORDER = ["duplicate_row", "duplicate_key", "range", "type", "outlier", "missing"]
# 页面排序：组级问题（整行/业务键重复）优先，其次列级缺失，最后单元格问题
KIND_ORDER = ["duplicate_row", "duplicate_key", "missing", "cell"]

SUGGESTION = {
    "missing": "补齐该字段：向上游补采或按规则判定为不适用后标注",
    "type": "按契约类型重新采集并回写，附原始值凭证",
    "range": "核对量纲/单位与采价口径，越界值退回上游重报",
    "outlier": "人工复核该值是否录入错误，抽样比价后确认或修正",
    "duplicate_key": "保留最新一条主数据，其余作废并回溯下游引用；跨供应商时先仲裁归属",
    "duplicate_row": "删除完全重复的行，保留首次入库记录；跨供应商时先仲裁归属",
}

ROW_UNIT = "（整行）"
DUP_KEY_UNIT = "（业务键）"
MAX_ORIGINAL_ROWS = 200
SCHEMA_VERSION = "attribution-1"


class AttributionError(Exception):
    """仲裁结论不合法或归集结果无法对账 → 拒绝切换，保留上一版。"""


# ---------------------------------------------------------------- 基础工具

def severity_rank(severity: str) -> int:
    return SEVERITY_RANK.get(severity, 0)


def _blank(value, tokens: tuple[str, ...]) -> bool:
    return str(value if value is not None else "").strip().lower() in tokens


def _rule_sort_key(rule: str) -> int:
    return RULE_ORDER.index(rule) if rule in RULE_ORDER else len(RULE_ORDER)


def _kind_sort_key(kind: str) -> int:
    return KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)


def _suggestion(rules: list[str]) -> str:
    parts: list[str] = []
    for rule in rules:
        text = SUGGESTION.get(rule)
        if text and text not in parts:
            parts.append(text)
    return "；".join(parts) if parts else "人工复核该问题"


def _problem_id(kind: str, key: str) -> str:
    """问题编号由「类型 + 业务键」决定，同一批数据重跑得到同一编号（可对账、可重试）。"""
    digest = hashlib.sha1(f"{kind}|{key}".encode("utf-8")).hexdigest()[:10]
    return f"Q-{kind[:4].upper()}-{digest}"


def _allocation_id(problem_id: str, supplier: str) -> str:
    digest = hashlib.sha1(f"{problem_id}|{supplier}".encode("utf-8")).hexdigest()[:10]
    return f"AL-{digest}"


def _supplier_map(rows: list[dict], tokens: tuple[str, ...], supplier_column: str) -> dict[str, str]:
    """row_id → 供应商；缺失/占位符视为未知（空串）。"""
    mapping: dict[str, str] = {}
    for row in rows:
        rid = str(row.get("row_id", ""))
        raw = row.get(supplier_column, "")
        mapping[rid] = "" if _blank(raw, tokens) else str(raw).strip()
    return mapping


def _owner_for(row_ids: list[str], suppliers: dict[str, str]) -> tuple[str | None, list[str]]:
    """候选供应商去重排序；0 个 → (None, [])，1 个 → (供应商, [供应商])，多个 → (None, 候选)。"""
    candidates = sorted({suppliers.get(rid, "") for rid in row_ids if suppliers.get(rid, "")})
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def _base_status(owner: str | None, candidates: list[str]) -> str:
    if owner:
        return STATUS_CLAIMED
    return STATUS_PENDING if len(candidates) > 1 else STATUS_UNASSIGNED


def _append_original(item: dict, row: dict) -> None:
    if len(item["original_rows"]) >= MAX_ORIGINAL_ROWS:
        item["original_rows_truncated"] = True
        return
    item["original_rows"].append(dict(row))


def _finish(item: dict) -> dict:
    item["supplier_candidates"] = sorted(set(item.get("supplier_candidates") or []))
    item["base_status"] = _base_status(item.get("base_owner"), item["supplier_candidates"])
    item["status"] = item["base_status"]
    item["owner"] = item.get("base_owner")
    item["source"] = "auto"
    item["resolution_note"] = ""
    item["rules"] = sorted(item["rules"], key=_rule_sort_key)
    item["rule_labels"] = [RULE_LABEL[r] for r in item["rules"]]
    item["rules_text"] = "、".join(item["rules"])
    item["merged_from"] = len(item["rules"])
    item["severity_label"] = SEVERITY_LABEL.get(item["severity"], item["severity"])
    item["reason"] = "；".join(item["reasons"])
    item["suggestion"] = _suggestion(item["rules"])
    item["fingerprint"] = _fingerprint(item)
    item["deduction"] = 0.0
    item["allocation_id"] = None
    _sync_allocation(item)
    return item


def _sync_allocation(item: dict) -> None:
    """只有已认领项才产生分配号；同问题 + 同供应商的分配号稳定不变。"""
    owner = item.get("owner")
    if item.get("status") == STATUS_CLAIMED and owner:
        item["allocation_id"] = _allocation_id(item["problem_id"], owner)
    else:
        item["allocation_id"] = None


def _fingerprint(item: dict) -> str:
    payload = {
        "kind": item["kind"],
        "row_id": item["row_id"],
        "column": item["column"],
        "rules": item["rules"],
        "group_rows": item.get("group_rows") or [],
        "row_ids": item.get("row_ids") or [],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 构建账本

def build(rows: list[dict], columns: list[str], rules: dict, missing: dict,
          duplicates: dict, outliers: dict, validation: dict) -> dict:
    """生成全部问题（每个问题一条），尚未套用人工仲裁。"""
    tokens = missing_tokens(rules)
    supplier_column = rules.get("supplier_column") or "supplier"
    suppliers = _supplier_map(rows, tokens, supplier_column)
    row_by_id = {str(row.get("row_id", "")): row for row in rows}
    items: list[dict] = []
    seen_cell: dict[tuple[str, str], dict] = {}

    def cell_item(row_id: str, column: str) -> dict:
        key = (row_id, column)
        item = seen_cell.get(key)
        if item is None:
            item = {
                "problem_id": _problem_id("cell", f"{row_id}|{column}"),
                "kind": "cell",
                "row_id": row_id,
                "column": column,
                "rules": [],
                "reasons": [],
                "severity": "low",
                "value": "",
                "scope": "cell",
                "group_rows": None,
                "rows_affected": 1,
                "row_ids": [row_id],
                "original_rows": [],
                "original_rows_truncated": False,
                "supplier_candidates": [],
                "base_owner": None,
            }
            seen_cell[key] = item
            items.append(item)
        original = row_by_id.get(row_id)
        if original is not None and not item["original_rows"]:
            _append_original(item, original)
        return item

    def add_rule(item: dict, rule: str, severity: str, reason: str, value: str = "") -> None:
        if rule not in item["rules"]:
            item["rules"].append(rule)
        if reason and reason not in item["reasons"]:
            item["reasons"].append(reason)
        if severity_rank(severity) > severity_rank(item["severity"]):
            item["severity"] = severity
        if value and not item["value"]:
            item["value"] = value

    # 1) 缺失：整列全空 → 一条列级问题（保留受影响行）；违规列 → 每个缺失单元格一条问题
    for stat in missing.get("columns") or []:
        if stat.get("status") not in ("violation", "all_empty"):
            continue
        severity = "high" if stat["status"] == "all_empty" else "medium"
        reason = stat.get("note") or "缺失率超过规则允许值"
        row_ids = _missing_row_ids(rows, stat["column"], tokens)
        if stat["status"] == "all_empty":
            item = {
                "problem_id": _problem_id("missing", stat["column"]),
                "kind": "missing",
                "row_id": "—",
                "column": stat["column"],
                "rules": [],
                "reasons": [],
                "severity": "low",
                "value": "",
                "scope": "column",
                "group_rows": None,
                "rows_affected": len(row_ids),
                "row_ids": row_ids,
                "original_rows": [],
                "original_rows_truncated": False,
                "supplier_candidates": [],
                "base_owner": None,
            }
            for rid in row_ids:
                if rid in row_by_id:
                    _append_original(item, row_by_id[rid])
            items.append(item)
            add_rule(item, "missing", severity, reason)
            owner, candidates = _owner_for(row_ids, suppliers)
            item["base_owner"], item["supplier_candidates"] = owner, candidates
            _finish(item)
            continue
        for rid in row_ids:
            item = cell_item(rid, stat["column"])
            add_rule(item, "missing", severity, reason)

    # 2) 类型 / 范围：单元格级，归入同一个 (row_id, column) 问题
    for cell in (validation.get("type") or {}).get("cells", []):
        item = cell_item(cell["row_id"], cell["column"])
        add_rule(item, "type", "medium",
                 f"类型错误：{cell.get('reason', '')}，期望 {cell.get('expected', '')}",
                 str(cell.get("value", "")))
    for cell in (validation.get("range") or {}).get("cells", []):
        item = cell_item(cell["row_id"], cell["column"])
        add_rule(item, "range", "medium", cell.get("reason", ""), str(cell.get("raw", "")))

    # 3) 异常值：IQR 与稳健 z 只产生一条单元格问题
    for cell in (outliers or {}).get("cells", []):
        item = cell_item(cell["row_id"], cell["column"])
        bounds = cell.get("bounds") or [None, None]
        robust_z = cell.get("robust_z")
        reason = (
            f"{cell.get('group', '')} 组内中位数 {cell.get('median')}，"
            f"IQR 区间 [{bounds[0]}, {bounds[1]}]，"
            f"稳健 z={robust_z if robust_z is not None else 'MAD=0 不可判'}"
        )
        add_rule(item, "outlier", cell.get("severity") or "medium", reason,
                 f"{cell.get('value')}")

    # 4) 整行重复：同一组只算一个行级问题
    for group in duplicates.get("duplicate_row", {}).get("groups", []):
        item = _group_item("duplicate_row", ROW_UNIT, group, row_by_id)
        add_rule(item, "duplicate_row", "high",
                 f"除 row_id 外全部列与同组其他行一致，本组 {group['count']} 行")
        owner, candidates = _owner_for(item["group_rows"], suppliers)
        item["base_owner"], item["supplier_candidates"] = owner, candidates
        items.append(item)
        _finish(item)

    # 5) 复合键重复：同一组只算一个问题（不再按行 × 键列展开）
    key_columns = duplicates.get("business_key") or []
    key_label = " + ".join(key_columns)
    for group in duplicates.get("duplicate_key", {}).get("groups", []):
        key_text = "、".join(f"{k}={v}" for k, v in (group.get("key") or {}).items())
        item = _group_item("duplicate_key", DUP_KEY_UNIT, group, row_by_id)
        add_rule(item, "duplicate_key", "medium",
                 f"业务键（{key_label}）重复：{key_text}，本组 {group['count']} 行，"
                 f"额外 {group['extra']} 行")
        owner, candidates = _owner_for(item["group_rows"], suppliers)
        item["base_owner"], item["supplier_candidates"] = owner, candidates
        items.append(item)
        _finish(item)

    # 6) 余下纯单元格问题：补齐供应商
    for item in items:
        if item.get("fingerprint"):
            continue
        owner, candidates = _owner_for([item["row_id"]], suppliers)
        item["base_owner"], item["supplier_candidates"] = owner, candidates
        _finish(item)

    items.sort(key=lambda a: (-severity_rank(a["severity"]), _kind_sort_key(a["kind"]),
                              a["column"], a["row_id"]))
    return {
        "schema": SCHEMA_VERSION,
        "items": items,
        "supplier_column": supplier_column,
        "suppliers": suppliers,
    }


def _missing_row_ids(rows: list[dict], column: str, tokens: tuple[str, ...]) -> list[str]:
    """缺失行的 row_id（按出现顺序去重：同一 row_id 重复出现也只算一个单元格问题）。"""
    seen: dict[str, None] = {}
    for row in rows:
        if _blank(row.get(column, ""), tokens):
            seen.setdefault(str(row.get("row_id", "")), None)
    return list(seen)


def _group_item(kind: str, column: str, group: dict, row_by_id: dict) -> dict:
    row_ids = [str(r) for r in group.get("row_ids") or []]
    item = {
        "problem_id": _problem_id(kind, "|".join(sorted(row_ids))),
        "kind": kind,
        "row_id": row_ids[0] if row_ids else "—",
        "column": column,
        "rules": [],
        "reasons": [],
        "severity": "low",
        "value": "",
        "scope": "row" if kind == "duplicate_row" else "group",
        "group_rows": row_ids,
        "rows_affected": len(row_ids),
        "row_ids": row_ids,
        "original_rows": [],
        "original_rows_truncated": False,
        "supplier_candidates": [],
        "base_owner": None,
    }
    for rid in row_ids:
        if rid in row_by_id:
            _append_original(item, row_by_id[rid])
    return item


# ---------------------------------------------------------------- 人工仲裁 / 重算

def apply_resolutions(ledger: dict, resolutions: dict[str, dict]) -> dict:
    """在自动判定之上套用仲裁结论。resolutions: problem_id → {decision, supplier, note}。

    先重置到 base_* 再逐条套用，保证同一批结论重复执行结果完全一致（幂等）。
    """
    items = ledger.get("items") or []
    by_id = {item["problem_id"]: item for item in items}
    unknown = sorted(set(resolutions) - set(by_id))
    if unknown:
        raise AttributionError("仲裁结论引用了不存在的问题编号：" + "、".join(unknown[:5]))

    for item in items:
        item["status"] = item["base_status"]
        item["owner"] = item["base_owner"]
        item["source"] = "auto"
        item["resolution_note"] = ""
        _sync_allocation(item)

    for problem_id, decision in resolutions.items():
        item = by_id[problem_id]
        action = (decision.get("decision") or "").strip()
        supplier = (decision.get("supplier") or "").strip()
        note = (decision.get("note") or "").strip()
        if action not in DECISIONS:
            raise AttributionError(f"问题 {problem_id} 的处理动作必须是 claim / pending / unassign。")
        if action == DECISION_CLAIM:
            if not supplier:
                raise AttributionError(f"问题 {problem_id} 选择认领时必须指定供应商。")
            item["status"] = STATUS_CLAIMED
            item["owner"] = supplier
            if supplier not in item["supplier_candidates"]:
                item["supplier_candidates"] = sorted(set(item["supplier_candidates"]) | {supplier})
        elif action == DECISION_PENDING:
            item["status"] = STATUS_PENDING
            item["owner"] = None
        else:
            item["status"] = STATUS_UNASSIGNED
            item["owner"] = None
        item["source"] = "resolution"
        item["resolution_note"] = note
        _sync_allocation(item)
    return ledger


def attach_penalties(ledger: dict, score_report: dict) -> dict:
    """把评分卡的扣分按「问题」分摊一次：Σ问题扣分 == 总分扣减。

    单元格规则：同一评分单元下的问题均分该单元扣分；
    重复规则：同一重复组只对应一条问题，整组只承接一次扣分。
    """
    items = ledger.get("items") or []
    for item in items:
        item["deduction"] = 0.0
        item["deduction_share"] = 0

    buckets: dict[tuple[str, str], list[dict]] = {}
    dup_buckets: dict[str, list[dict]] = {"duplicate_key": [], "duplicate_row": []}
    for item in items:
        if item["kind"] in dup_buckets:
            dup_buckets[item["kind"]].append(item)
            continue
        for rule in item["rules"]:
            buckets.setdefault((item["column"], rule), []).append(item)

    total_deduction = 0.0
    for unit in score_report.get("items") or []:
        deduction = float(unit.get("deduction") or 0.0)
        if deduction <= 0:
            continue
        total_deduction += deduction
        rule = unit["rule"]
        if rule in dup_buckets:
            targets = dup_buckets[rule]
        else:
            targets = buckets.get((unit["column"], rule), [])
        if not targets:
            # 评分单元有扣分但账本没有对应问题：保留为「未归属扣分」，由对账报错
            ledger.setdefault("unattributed_units", []).append({
                "column": unit["column"],
                "rule": rule,
                "deduction": round(deduction, 6),
                "reason": "评分单元找不到对应问题",
            })
            continue
        share = deduction / len(targets)
        for item in targets:
            item["deduction"] += share
            item["deduction_share"] += 1

    for item in items:
        item["deduction"] = round(item["deduction"], 6)

    ledger["deduction_total"] = round(total_deduction, 6)
    ledger["score_total"] = score_report.get("total")
    ledger["score_deduction"] = round(100.0 - float(score_report.get("total") or 0.0), 6)
    return ledger


def finalize(ledger: dict, resolutions: dict[str, dict] | None = None) -> dict:
    """套用仲裁、汇总、生成批次键与供应商结果。

    ``resolutions`` 缺省时使用账本自带的 ``resolutions``，保证「改扣分后重算」不会
    丢掉已经生效的仲裁结论。
    """
    if resolutions is None:
        resolutions = ledger.get("resolutions") or {}
    ledger["resolutions"] = {k: dict(v) for k, v in resolutions.items()}
    ledger = apply_resolutions(ledger, resolutions)
    items = ledger["items"]
    summary = _summarize(items, ledger.get("deduction_total", 0.0))
    ledger["summary"] = summary
    ledger["batch_key"] = _batch_key(items)
    ledger["content_key"] = _content_key(items)
    ledger["item_count"] = len(items)
    return ledger


def _content_key(items: list[dict]) -> str:
    """内容指纹：含去向与扣分。同一批重试内容完全一致 → 同一个 content_key。"""
    payload = sorted(
        [item["problem_id"], item["status"], item.get("owner") or "",
         round(float(item.get("deduction") or 0.0), 6)]
        for item in items
    )
    raw = json.dumps([SCHEMA_VERSION, payload], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def reprice(ledger: dict, score_report: dict,
            resolutions: dict[str, dict] | None = None) -> dict:
    """按新的评分卡重新分摊扣分（改权重后重算）。

    归集底账（问题集合、原始行、自动判定）保持不变，只重算扣分并重新套用仲裁，
    因此重算不会新增问题、不会新增分配。
    """
    base = copy.deepcopy(ledger)
    base.pop("summary", None)
    base.pop("batch_key", None)
    base.pop("unattributed_units", None)
    base = attach_penalties(base, score_report)
    base["resolutions"] = dict(resolutions if resolutions is not None
                               else ledger.get("resolutions") or {})
    return finalize(base)


def _batch_key(items: list[dict]) -> str:
    payload = [
        [item["problem_id"], item["status"], item.get("owner") or ""]
        for item in items
    ]
    payload.sort()
    raw = json.dumps([SCHEMA_VERSION, payload], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _summarize(items: list[dict], deduction_total: float) -> dict:
    buckets = {status: 0 for status in STATUSES}
    deductions = {status: 0.0 for status in STATUSES}
    suppliers: dict[str, dict] = {}
    pending_candidates: dict[str, int] = {}
    for item in items:
        status = item["status"]
        buckets[status] = buckets.get(status, 0) + 1
        deductions[status] = deductions.get(status, 0.0) + float(item.get("deduction") or 0.0)
        if status == STATUS_CLAIMED:
            owner = item["owner"]
            entry = suppliers.setdefault(owner, {
                "supplier": owner,
                "problems": 0,
                "deduction": 0.0,
                "high": 0,
                "medium": 0,
                "low": 0,
                "allocation_ids": [],
            })
            entry["problems"] += 1
            entry["deduction"] += float(item.get("deduction") or 0.0)
            entry[item["severity"]] = entry.get(item["severity"], 0) + 1
            if item.get("allocation_id"):
                entry["allocation_ids"].append(item["allocation_id"])
        elif status == STATUS_PENDING:
            for candidate in item.get("supplier_candidates") or []:
                pending_candidates[candidate] = pending_candidates.get(candidate, 0) + 1

    supplier_list = []
    for entry in suppliers.values():
        entry["deduction"] = round(entry["deduction"], 6)
        entry["allocation_count"] = len(entry["allocation_ids"])
        entry["allocation_ids"] = sorted(entry["allocation_ids"])
        supplier_list.append(entry)
    supplier_list.sort(key=lambda e: (-e["deduction"], e["supplier"]))

    claimed_deduction = round(deductions[STATUS_CLAIMED], 6)
    pending_deduction = round(deductions[STATUS_PENDING], 6)
    unassigned_deduction = round(deductions[STATUS_UNASSIGNED], 6)
    return {
        "total": len(items),
        "claimed": buckets[STATUS_CLAIMED],
        "pending": buckets[STATUS_PENDING],
        "unassigned": buckets[STATUS_UNASSIGNED],
        "claim_rate": round(buckets[STATUS_CLAIMED] / len(items), 6) if items else 0.0,
        "deduction_total": round(float(deduction_total), 6),
        "deduction_claimed": claimed_deduction,
        "deduction_pending": pending_deduction,
        "deduction_unassigned": unassigned_deduction,
        "deduction_supplier_sum": round(sum(e["deduction"] for e in supplier_list), 6),
        "allocation_count": buckets[STATUS_CLAIMED],
        "suppliers": supplier_list,
        "pending_candidates": dict(sorted(pending_candidates.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_status": [
            {"status": status, "label": STATUS_LABEL[status], "count": buckets[status],
             "deduction": round(deductions[status], 6)}
            for status in STATUSES
        ],
    }


# ---------------------------------------------------------------- 对账

def reconcile(ledger: dict) -> dict:
    """发布前对账。任何一项不通过，调用方必须保留上一版。"""
    items = ledger.get("items") or []
    summary = ledger.get("summary") or _summarize(items, ledger.get("deduction_total", 0.0))
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    ids = [item["problem_id"] for item in items]
    check("问题编号唯一", len(ids) == len(set(ids)),
          f"{len(ids)} 条问题，{len(set(ids))} 个唯一编号")

    fingerprints = [item.get("fingerprint") for item in items]
    check("问题不重复入账", len(fingerprints) == len(set(fingerprints)),
          f"{len(set(fingerprints))} 个唯一指纹")

    bucket_sum = sum(1 for item in items if item["status"] in STATUSES)
    check("三桶互斥且合计等于问题总数",
          bucket_sum == len(items)
          and summary["claimed"] + summary["pending"] + summary["unassigned"] == len(items),
          f"已认领 {summary['claimed']} + 待仲裁 {summary['pending']} + "
          f"未分配 {summary['unassigned']} = {len(items)}")

    bad_owner = [item["problem_id"] for item in items
                 if (item["status"] == STATUS_CLAIMED and not item.get("owner"))
                 or (item["status"] != STATUS_CLAIMED and item.get("owner"))]
    check("负责人与去向一致", not bad_owner,
          "已认领必须有负责人，挂起/未分配不得预占供应商" if not bad_owner
          else f"{len(bad_owner)} 条状态与负责人不一致")

    allocations = [item["allocation_id"] for item in items if item.get("allocation_id")]
    check("分配号唯一", len(allocations) == len(set(allocations)),
          f"{len(allocations)} 个分配号")

    dup_ids: dict[str, str] = {}
    dup_dupe = []
    for item in items:
        if item["kind"] not in ("duplicate_key", "duplicate_row"):
            continue
        marker = f"{item['kind']}|{'|'.join(sorted(item.get('group_rows') or []))}"
        if marker in dup_ids:
            dup_dupe.append(item["problem_id"])
        else:
            dup_ids[marker] = item["problem_id"]
    check("重复组只计一次", not dup_dupe,
          f"{len(dup_ids)} 个重复组，各计一条问题" if not dup_dupe
          else f"{len(dup_dupe)} 个重复组被重复计入")

    supplier_problem_sum = sum(entry["problems"] for entry in summary["suppliers"])
    check("供应商结果不重复计入", supplier_problem_sum == summary["claimed"],
          f"供应商问题数合计 {supplier_problem_sum} = 已认领 {summary['claimed']}")

    total = float(ledger.get("deduction_total") or 0.0)
    item_sum = sum(float(item.get("deduction") or 0.0) for item in items)
    score_total = float(ledger.get("score_total") or 0.0)
    check("问题扣分合计等于总分扣减",
          abs(item_sum - total) <= 0.011 and abs((100.0 - score_total) - total) <= 0.011,
          f"问题扣分 {item_sum:.6f}，总分扣减 {total:.6f}，100 − 总分 {100.0 - score_total:.6f}")

    status_deduction = (summary["deduction_claimed"] + summary["deduction_pending"]
                        + summary["deduction_unassigned"])
    check("三桶扣分合计无遗漏", abs(status_deduction - total) <= 0.011,
          f"已认领 {summary['deduction_claimed']:.6f} + 待仲裁 "
          f"{summary['deduction_pending']:.6f} + 未分配 "
          f"{summary['deduction_unassigned']:.6f} = {status_deduction:.6f}")

    unattributed = ledger.get("unattributed_units") or []
    check("评分扣分全部有归属", not unattributed,
          "全部评分单元都对应到问题" if not unattributed
          else f"{len(unattributed)} 个评分单元无对应问题")

    return {
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "errors": [f"{item['name']}：{item['detail']}" for item in checks if not item["ok"]],
    }
