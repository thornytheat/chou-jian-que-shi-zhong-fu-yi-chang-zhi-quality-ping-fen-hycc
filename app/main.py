"""FastAPI 路由（薄）：参数解析 + 调用剖析流水线 + 渲染页面。

核心计算都在 missing / duplicates / outliers / score / alerts / validators 里，
路由只负责：取输入、起后台线程、读 SQLite 快照、套模板。
"""
from __future__ import annotations

import pathlib
import threading
import uuid

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import attribution, export, loader, pipeline, score, seed, settings, store
from . import alerts as alerts_view
BASE_DIR = pathlib.Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["pct"] = lambda v, d=1: "—" if v is None else f"{float(v) * 100:.{d}f}%"
templates.env.filters["num"] = lambda v, d=2: "—" if v is None else f"{float(v):.{d}f}"
templates.env.filters["sev"] = lambda s: {"high": "err", "medium": "warn", "low": ""}.get(s, "")

@asynccontextmanager
async def lifespan(_: FastAPI):
    store.connect()
    ensure_seeded()
    yield


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

ALERT_PAGE_LIMIT = 80
_STATE: dict = {"rules": None, "seeded": False}
_LOCK = threading.Lock()


def rules() -> dict:
    if _STATE["rules"] is None:
        _STATE["rules"] = loader.load_rules()
    return _STATE["rules"]


def ensure_seeded() -> None:
    if _STATE["seeded"]:
        return
    with _LOCK:
        if _STATE["seeded"]:
            return
        seed.ensure_seed()
        _STATE["seeded"] = True


# ---------------------------------------------------------------- 页面

def column_specs(r: dict) -> list[dict]:
    """把 rules.json 的列定义投影成模板可直接渲染的只读结构（不含任何计算）。"""
    return [
        {
            "name": name,
            "label": spec.get("label", name),
            "type": spec.get("type"),
            "weight": spec.get("weight"),
            "max_missing_rate": spec.get("max_missing_rate"),
            "rules": [score.RULE_LABELS[x] for x in spec.get("rules", [])],
            "range": spec.get("range"),
        }
        for name, spec in r["columns"].items()
    ]



@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    ensure_seeded()
    r = rules()
    history = store.list_runs(limit=5)
    latest = history[0] if history else None
    latest_report = (store.get_run(latest["run_id"]) or {}).get("report") if latest else None
    latest_report = latest_report or {}
    # 首页统计：只读最近一次运行的报告快照，没有运行记录时全部为 None
    latest_ledger = (latest_report.get("attribution") or {}) if latest_report else {}
    latest_summary = latest_ledger.get("summary") or {}
    stats = {
        "runs": store.count_runs(),
        "total": latest.get("total_score") if latest else None,
        "grade": latest.get("grade") if latest else None,
        "alerts": (latest_report.get("alerts") or {}).get("total"),
        "high": (latest_report.get("alerts") or {}).get("high"),
        "medium": (latest_report.get("alerts") or {}).get("medium"),
        "merged_cells": (latest_report.get("alerts") or {}).get("merged_cells"),
        "duplicate_groups": ((latest_report.get("duplicates") or {}).get("duplicate_key") or {}).get("group_count"),
        "row_groups": ((latest_report.get("duplicates") or {}).get("duplicate_row") or {}).get("group_count"),
        "outlier_cells": len((latest_report.get("outliers") or {}).get("cells") or []),
        "insufficient": len((latest_report.get("outliers") or {}).get("insufficient") or []),
        "claimed": latest_summary.get("claimed"),
        "pending": latest_summary.get("pending"),
        "unassigned": latest_summary.get("unassigned"),
        "problems": latest_summary.get("total"),
    }
    return templates.TemplateResponse(request, "index.html", {
        "app_name": settings.APP_NAME,
        "page": "index",
        "rules": r,
        "columns": column_specs(r),
        "history": history,
        "latest": latest,
        "latest_report": latest_report,
        "stats": stats,
        "business_key": r.get("business_key"),
        "rule_weights": r.get("rule_weights", {}),
        "rule_labels": score.RULE_LABELS,
        "thresholds": r.get("grade_thresholds"),
        "outlier": r.get("outlier"),
        "min_group_sample": r.get("min_group_sample"),
        "sample_rows": 2000,
    })


@app.get("/workbench", response_class=HTMLResponse)
def workbench(request: Request):
    ensure_seeded()
    r = rules()
    history = store.list_runs(limit=5)
    return templates.TemplateResponse(request, "workbench.html", {
        "app_name": settings.APP_NAME,
        "page": "workbench",
        "columns": list(r["columns"].keys()),
        "column_specs": column_specs(r),
        "rule_weights": r.get("rule_weights", {}),
        "rule_labels": score.RULE_LABELS,
        "rules_version": r.get("version"),
        "business_key": r.get("business_key"),
        "group_by": r.get("group_by"),
        "outlier": r.get("outlier"),
        "min_group_sample": r.get("min_group_sample"),
        "thresholds": r.get("grade_thresholds"),
        "sample_rows": 2000,
        "history": history,
        "latest": history[0] if history else None,
        "stages": [label for _, label in pipeline.STAGES],
    })


@app.get("/result/{run_id}", response_class=HTMLResponse)
def result(request: Request, run_id: str, limit: int = ALERT_PAGE_LIMIT):
    ensure_seeded()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在，请回到工作台重新执行。")
    if run["status"] == "running":
        return templates.TemplateResponse(request, "running.html", {
            "app_name": settings.APP_NAME,
            "page": "result",
            "run": run,
        })
    if run["status"] == "failed":
        raise HTTPException(status_code=409, detail=f"该次运行失败：{run.get('error') or '未知错误'}")
    report = run["report"]
    limit = max(10, min(500, limit))
    # 页面以「生效归集版本」为准（仲裁后可能新于报告快照），没有版本时退回报告内的账本
    active = store.get_attribution(run_id)
    ledger = (active or {}).get("ledger") or report.get("attribution") or {}
    reconcile = (active or {}).get("reconcile") or report.get("reconcile") or {}
    alert_view = alerts_view.build(ledger) if ledger.get("items") else report["alerts"]
    total_alerts = alert_view["total"]
    ledger_limit = max(10, min(300, limit))
    ledger_items = (ledger.get("items") or [])[:ledger_limit]
    return templates.TemplateResponse(request, "result.html", {
        "app_name": settings.APP_NAME,
        "page": "result",
        "run": run,
        "report": report,
        "columns": report["dataset"]["column_names"],
        "score": report["score"],
        "alerts_view": alert_view,
        "alerts": alert_view["items"][:limit],
        "alerts_shown": min(limit, total_alerts),
        "alerts_total": total_alerts,
        "alert_limit": limit,
        "insufficient": report["outliers"]["insufficient"],
        "rule_labels": score.RULE_LABELS,
        "severity_labels": {"high": "高", "medium": "中", "low": "低"},
        "ledger": ledger,
        "ledger_items": ledger_items,
        "ledger_summary": ledger.get("summary") or {},
        "ledger_limit": ledger_limit,
        "ledger_shown": len(ledger_items),
        "reconcile": reconcile,
        "attribution_versions": store.list_attributions(run_id, limit=8),
        "allocation_count": store.allocation_count(run_id),
        "supplier_column": ledger.get("supplier_column") or "supplier",
    })


@app.get("/review", response_class=HTMLResponse)
def review(request: Request):
    ensure_seeded()
    r = rules()
    overrides = store.weight_overrides()
    columns, rule_weights = score.merged_weights(r, overrides)
    history = store.list_runs(limit=20)
    recent = history[0] if history else None
    return templates.TemplateResponse(request, "review.html", {
        "app_name": settings.APP_NAME,
        "page": "review",
        "rules": r,
        "history": history,
        "recent": recent,
        "columns": [
            {"name": n, "label": s.get("label", n), "default": float(s.get("weight", 1.0)),
             "value": columns[n]}
            for n, s in r["columns"].items()
        ],
        "rule_weights": [
            {"name": k, "label": score.RULE_LABELS.get(k, k), "default": float(v),
             "value": rule_weights[k]}
            for k, v in rule_weights.items()
        ],
        "has_overrides": bool(overrides.get("columns") or overrides.get("rule_weights")),
        "override_count": len(overrides.get("columns") or {}) + len(overrides.get("rule_weights") or {}),
        "thresholds": r.get("grade_thresholds"),
    })


# ---------------------------------------------------------------- API


def _start_run(source: str, data: dict) -> dict:
    r = rules()
    overrides = store.weight_overrides()
    run_id = uuid.uuid4().hex[:12]
    store.create_run(run_id, source, overrides)
    rows, columns = data["rows"], data["columns"]

    def worker() -> None:
        try:
            def progress(index: int, label: str) -> None:
                store.set_progress(run_id, index, label)

            report = pipeline.run(rows, columns, r, overrides, progress)
            report["run_id"] = run_id
            report["dataset"]["source"] = source
            report["dataset"]["encoding"] = data.get("encoding", "utf-8")
            store.finish_run(run_id, report)
        except Exception as exc:  # noqa: BLE001 - 记录失败原因给页面看
            store.fail_run(run_id, f"{type(exc).__name__}: {exc}")

    threading.Thread(target=worker, name=f"run-{run_id}", daemon=True).start()
    return {"run_id": run_id, "status": "running", "stages": [s[1] for s in pipeline.STAGES]}


@app.post("/api/runs")
def create_run(payload: dict) -> dict:
    ensure_seeded()
    source = (payload or {}).get("source", "sample")
    if source != "sample":
        raise HTTPException(status_code=400, detail="source 只支持 sample；上传请使用 /api/runs/upload。")
    r = rules()
    try:
        data = loader.load_sample(r)
    except loader.InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _start_run(data["source"], data)


@app.post("/api/runs/upload")
async def create_run_upload(file: UploadFile = File(...)) -> dict:
    ensure_seeded()
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传的文件为空，请选择含表头与数据行的 CSV。")
    if len(raw) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 8MB 上限，请分批抽检。")
    r = rules()
    try:
        text, encoding = loader.decode_bytes(raw)
        data = loader.load_csv_text(text, r, f"上传文件 {file.filename or 'upload.csv'}")
    except loader.InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    data["encoding"] = encoding
    return _start_run(data["source"], data)


@app.post("/api/runs/text")
def create_run_text(payload: dict) -> dict:
    """粘贴 CSV 文本执行（同样走严格表头校验）。"""
    ensure_seeded()
    text = (payload or {}).get("csv_text") or ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="CSV 文本为空，请粘贴含表头的内容。")
    r = rules()
    try:
        data = loader.load_csv_text(text, r, "粘贴的 CSV 文本")
    except loader.InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _start_run(data["source"], data)


@app.get("/api/runs/{run_id}/status")
def run_status(run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    stages = [{"name": name, "label": label} for name, label in pipeline.STAGES]
    report = run.get("report") or {}
    return {
        "run_id": run_id,
        "status": run["status"],
        "stage": run["stage"],
        "stage_index": run["stage_index"],
        "stage_total": len(stages),
        "stages": stages,
        "rows": run["rows"],
        "total_score": run["total_score"],
        "grade": run["grade"],
        "error": run.get("error"),
        "alerts": (report.get("alerts") or {}).get("total"),
        "elapsed_ms": report.get("elapsed_ms"),
        "result_url": f"/result/{run_id}",
    }


@app.get("/api/runs/{run_id}/alerts.csv", response_class=PlainTextResponse)
def alerts_csv(run_id: str):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    if run["status"] != "done" or not run.get("report"):
        raise HTTPException(status_code=409, detail="该次运行尚未完成，暂不能导出工单。")
    # 工单以生效归集版本为准：仲裁后供应商/去向跟着变，问题本身不重复
    active = store.get_attribution(run_id)
    ledger = (active or {}).get("ledger") or run["report"].get("attribution")
    alerts_payload = alerts_view.build(ledger) if ledger else run["report"]["alerts"]
    csv_text = export.alerts_to_csv(alerts_payload)
    filename = f"qualiscan-workorder-{run_id}.csv"
    return PlainTextResponse(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/rules/weights")
def get_weights() -> dict:
    r = rules()
    overrides = store.weight_overrides()
    columns, rule_weights = score.merged_weights(r, overrides)
    return {
        "columns": columns,
        "rule_weights": rule_weights,
        "defaults": {
            "columns": {n: float(s.get("weight", 1.0)) for n, s in r["columns"].items()},
            "rule_weights": {k: float(v) for k, v in r.get("rule_weights", {}).items()},
        },
        "overrides": overrides,
    }


@app.post("/api/rules/weights")
def set_weights(payload: dict) -> dict:
    r = rules()
    payload = payload or {}
    raw_columns = payload.get("columns") or {}
    raw_rules = payload.get("rule_weights") or {}
    columns: dict[str, float] = {}
    rule_weights: dict[str, float] = {}
    for name, value in raw_columns.items():
        if name not in r["columns"]:
            raise HTTPException(status_code=400, detail=f"未知列 {name}，无法设置权重。")
        columns[name] = _parse_weight(value, f"列 {name} 权重")
    for name, value in raw_rules.items():
        if name not in r.get("rule_weights", {}):
            raise HTTPException(status_code=400, detail=f"未知规则 {name}，无法设置权重。")
        rule_weights[name] = _parse_weight(value, f"规则 {name} 权重")
    saved = store.save_weight_overrides(columns, rule_weights)
    merged_columns, merged_rules = score.merged_weights(r, saved)
    return {"ok": True, "columns": merged_columns, "rule_weights": merged_rules}


def _parse_weight(value, label: str) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{label} 必须是数字。") from exc
    if weight < 0 or weight > 10:
        raise HTTPException(status_code=400, detail=f"{label} 需在 0 ~ 10 之间。")
    return round(weight, 3)


@app.post("/api/rules/weights/reset")
def reset_weights() -> dict:
    store.save_weight_overrides({}, {})
    return {"ok": True}


@app.post("/api/runs/{run_id}/rescore")
def rescore(run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    if run["status"] != "done" or not run.get("report"):
        raise HTTPException(status_code=409, detail="该次运行尚未完成，无法重算。")
    r = rules()
    overrides = store.weight_overrides()
    report = _rescore_report(run, r, overrides)
    ledger = report["attribution"]
    reconcile = report["reconcile"]
    try:
        published = store.publish_attribution(
            run_id, ledger, reconcile, report.get("resolutions"), report,
        )
    except store.StoreError as exc:
        _reject(run_id, ledger, reconcile, report.get("resolutions"), str(exc))
        raise HTTPException(status_code=409, detail=f"重算未通过对账，已保留上一版：{exc}") from exc
    return {
        "run_id": run_id,
        "total": report["score"]["total"],
        "grade": report["score"]["grade"],
        "weights": report["score"]["weights_used"],
        "version_id": published.get("version_id"),
        "batch_key": ledger.get("batch_key"),
        "summary": ledger.get("summary"),
    }


def _rescore_report(run: dict, r: dict, overrides: dict) -> dict:
    """按当前权重重算评分卡，并把扣分重新分摊到同一份归集底账上（不改问题集合）。

    仲裁结论以生效归集版本为准（报告快照里的 resolutions 可能落后），重算后重新套用，
    因此改权重不会把已认领的问题打回待仲裁，也不会新增分配。
    """
    report = run["report"]
    units = score.build_units(report["missing"], report["duplicates"],
                              report["outliers"], report["validation"], r)
    report["score"] = score.compute(units, r, overrides)
    base_ledger = report.get("attribution")
    if base_ledger:
        active = store.get_attribution(run["run_id"])
        resolutions = ((active or {}).get("resolutions")
                       or base_ledger.get("resolutions")
                       or report.get("resolutions") or {})
        ledger = attribution.reprice(base_ledger, report["score"], resolutions)
        report["attribution"] = ledger
        report["resolutions"] = dict(resolutions)
        report["alerts"] = alerts_view.build(ledger)
        report["reconcile"] = attribution.reconcile(ledger)
    return report


def _reject(run_id: str, ledger: dict | None, reconcile: dict | None,
            resolutions: dict | None, message: str) -> str:
    try:
        return store.record_failed_attribution(run_id, ledger, reconcile or {}, resolutions, message)
    except Exception:  # noqa: BLE001 - 留痕失败不应遮蔽原始错误
        return ""


# ---------------------------------------------------------------- 责任归集

@app.get("/api/runs/{run_id}/attribution")
def attribution_api(run_id: str, status: str | None = None, version: str | None = None) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    if run["status"] != "done" or not run.get("report"):
        raise HTTPException(status_code=409, detail="该次运行尚未完成，暂无责任归集结果。")
    active = store.get_attribution(run_id, version)
    ledger = (active or {}).get("ledger") or run["report"].get("attribution") or {}
    items = ledger.get("items") or []
    if status:
        if status not in attribution.STATUSES:
            raise HTTPException(status_code=400, detail="status 只支持 claimed / pending / unassigned。")
        items = [item for item in items if item["status"] == status]
    return {
        "run_id": run_id,
        "version_id": (active or {}).get("version_id"),
        "batch_key": ledger.get("batch_key"),
        "summary": ledger.get("summary"),
        "items": items,
        "reconcile": (active or {}).get("reconcile") or run["report"].get("reconcile"),
        "allocations": store.allocation_count(run_id),
    }


@app.post("/api/runs/{run_id}/attribution/resolve")
def attribution_resolve(run_id: str, payload: dict) -> dict:
    """提交仲裁结论：在底账上重新判定去向，对账通过后原子切换生效版本。"""
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    if run["status"] != "done" or not run.get("report"):
        raise HTTPException(status_code=409, detail="该次运行尚未完成，暂不能提交仲裁。")
    report = run["report"]
    base_ledger = report.get("attribution") or {}
    if not base_ledger.get("items"):
        raise HTTPException(status_code=409, detail="该次运行没有责任归集底账，请重新执行抽检。")

    payload = payload or {}
    incoming = payload.get("resolutions") or {}
    if payload.get("problem_id"):
        incoming = {payload["problem_id"]: {
            "decision": payload.get("decision"),
            "supplier": payload.get("supplier"),
            "note": payload.get("note"),
        }}
    if not isinstance(incoming, dict) or not incoming:
        raise HTTPException(status_code=400, detail="请至少提交一条仲裁结论（problem_id + decision）。")

    merged = dict(base_ledger.get("resolutions") or {})
    merged.update(incoming)

    try:
        ledger = attribution.reprice(base_ledger, report["score"], merged)
    except attribution.AttributionError as exc:
        _reject(run_id, base_ledger, report.get("reconcile"), merged, str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    reconcile = attribution.reconcile(ledger)
    try:
        published = store.publish_attribution(run_id, ledger, reconcile, merged, None)
    except store.StoreError as exc:
        _reject(run_id, ledger, reconcile, merged, str(exc))
        raise HTTPException(status_code=409, detail=f"归集未通过对账，已保留上一版：{exc}") from exc

    return {
        "run_id": run_id,
        "ok": True,
        "version_id": published.get("version_id"),
        "batch_key": ledger.get("batch_key"),
        "summary": ledger.get("summary"),
        "reconcile": reconcile,
        "allocations": store.allocation_count(run_id),
        "items": [item for item in ledger["items"]
                  if item["problem_id"] in incoming],
    }


@app.post("/api/runs/{run_id}/attribution/retry")
def attribution_retry(run_id: str) -> dict:
    """重试当前这批归集：同一批输入 + 同一批仲裁结论 → 同一个 batch_key，不新增分配。"""
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {run_id} 不存在。")
    if run["status"] != "done" or not run.get("report"):
        raise HTTPException(status_code=409, detail="该次运行尚未完成，无法重试。")
    report = run["report"]
    active = store.get_attribution(run_id)
    base_ledger = report.get("attribution") or {}
    if not base_ledger.get("items"):
        raise HTTPException(status_code=409, detail="该次运行没有责任归集底账，请重新执行抽检。")
    resolutions = (active or {}).get("resolutions") or base_ledger.get("resolutions") or {}
    before = store.allocation_count(run_id)

    ledger = attribution.reprice(base_ledger, report["score"], resolutions)
    reconcile = attribution.reconcile(ledger)
    try:
        published = store.publish_attribution(run_id, ledger, reconcile, resolutions, None)
    except store.StoreError as exc:
        _reject(run_id, ledger, reconcile, resolutions, str(exc))
        raise HTTPException(status_code=409, detail=f"重试未通过对账，已保留上一版：{exc}") from exc

    after = store.allocation_count(run_id)
    same_batch = (active or {}).get("batch_key") == ledger.get("batch_key")
    return {
        "run_id": run_id,
        "ok": True,
        "version_id": published.get("version_id"),
        "batch_key": ledger.get("batch_key"),
        "same_batch": same_batch,
        "allocations_before": before,
        "allocations_after": after,
        "allocations_added": after - before,
        "summary": ledger.get("summary"),
        "reconcile": reconcile,
    }


@app.get("/health")
def health() -> JSONResponse:
    ensure_seeded()
    return JSONResponse({
        "status": "ok",
        "app": settings.APP_SHORT,
        "runs": store.count_runs(),
        "rules_version": rules().get("version"),
        "data_dir": str(settings.DATA_DIR),
    })


@app.get("/api/rules")
def rules_api() -> dict:
    return rules()


@app.get("/api/runs")
def runs_api(limit: int = 20) -> dict:
    return {"runs": store.list_runs(limit=max(1, min(100, limit)))}
