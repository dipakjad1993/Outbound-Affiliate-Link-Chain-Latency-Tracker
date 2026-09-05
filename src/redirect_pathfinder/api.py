"""FastAPI — enterprise REST surface + 3-page web app host + live jobs + docs + PDF."""
from __future__ import annotations
import asyncio
import re
import time as _time
import uuid
from pathlib import Path
import httpx
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from .orchestrator import load_config, audit_single_url, run_bulk_audit
from .revenue import network_sla_matrix
from .alerting_export import build_alerts, jira_tickets
from .input_ingest import fetch_sitemap_urls
from .autofill import profile_site, _check_sitemap
from .site_content import SECTIONS, NAV
from .export_pdf import build_pdf

app = FastAPI(title="Outbound Affiliate Link Chain & Latency Tracker", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
CFG_PATH = "config/enterprise.yaml"

_ROOT = Path(__file__).resolve().parent.parent.parent
_FRONTEND = _ROOT / "frontend"
if not _FRONTEND.exists():
    _FRONTEND = Path("frontend").resolve()
if _FRONTEND.exists():
    app.mount("/app", StaticFiles(directory=str(_FRONTEND), html=True), name="app")


class SingleAuditIn(BaseModel):
    source_url: str
    anchor_text: str = ""
    expected_operator: str = ""
    expected_network: str = ""
    geo: str = "US-NJ"
    device: str = "desktop_chrome"
    clicks_30d: float = 1000
    epc: float = 1.25
    bonus_text_on_site: str = ""


class FullAuditIn(BaseModel):
    targets: list[SingleAuditIn] = []
    pasted_urls: str = ""
    sitemap_urls: list[str] = []
    sitemap_limit: int = 50
    global_geo: str = "US-NJ"
    global_device: str = "desktop_chrome"
    settings: dict = {}


class AutofillIn(BaseModel):
    site_url: str
    max_pages: int = 25


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        elif v is not None:
            base[k] = v
    return base


def _clean_lines(text) -> list[str]:
    """Field lines minus blanks, #-comment lines and trailing '  # status' notes."""
    lines = text if isinstance(text, list) else (text or "").splitlines()
    out = []
    for ln in lines:
        s = (ln or "").strip()
        if not s or s.startswith("#"):
            continue
        s = re.sub(r"\s{2,}#.*$", "", s).strip().strip(",")
        if s:
            out.append(s)
    return out


async def _expand_rows(body: FullAuditIn, cfg: dict, log=None) -> list[dict]:
    rows = [r.model_dump() for r in body.targets]
    if log:
        log(f"seeded {len(rows)} table targets")
    n_paste = 0
    for u in _clean_lines(body.pasted_urls):
        if u.startswith("http"):
            rows.append({"source_url": u, "geo": body.global_geo, "device": body.global_device,
                         "clicks_30d": 1000, "epc": cfg.get("revenue", {}).get("default_epc", 1.25)})
            n_paste += 1
    if log:
        log(f"added {n_paste} pasted URLs")
    for sm in _clean_lines(body.sitemap_urls):
        if log:
            log(f"expanding sitemap {sm} …")
        urls = await fetch_sitemap_urls(sm, limit=body.sitemap_limit)
        if log:
            log(f"sitemap yielded {len(urls)} URLs")
        for u in urls:
            rows.append({"source_url": u, "geo": body.global_geo, "device": body.global_device,
                         "clicks_30d": 500, "epc": cfg.get("revenue", {}).get("default_epc", 1.25)})
    return rows


def _bundle(chains, cfg) -> dict:
    sla = network_sla_matrix(chains)
    alerts = []
    for c in chains:
        alerts.extend(build_alerts(c, int(cfg.get("alerting", {}).get("break_on_high_value_clicks", 5000))))
    return {"chains": [c.model_dump() for c in chains], "sla": sla, "alerts": alerts,
            "jira": jira_tickets(chains),
            "revenue_at_risk": round(sum(c.revenue_at_risk for c in chains), 2),
            "health_index": round(sum(c.health_score for c in chains) / max(1, len(chains)), 1)}


# ---------------- live jobs (log + timer) ----------------
JOBS: dict = {}


async def _run_job(jid: str, payload: dict):
    job = JOBS[jid]
    t0 = job["t0"]

    def log(msg: str):
        job["logs"].append(f"[{_time.perf_counter() - t0:7.1f}s] {msg}")

    try:
        body = FullAuditIn(**payload)
        cfg = _deep_merge(load_config(CFG_PATH), body.settings or {})
        log(f"engine start — mode={cfg.get('audit', {}).get('execution_mode')} "
            f"max_hops={cfg.get('audit', {}).get('max_hops')} "
            f"concurrency={cfg.get('audit', {}).get('concurrency')}")
        rows = await _expand_rows(body, cfg, log=log)
        if not rows:
            job.update(status="error", error="no targets — add URLs, paste a list, upload CSV or give a sitemap")
            return
        job["total"] = len(rows)
        deep = cfg.get("audit", {}).get("deep_cross_device", "auto")
        cfg["audit"]["_deep_xdevice"] = (len(rows) <= 12) if deep == "auto" else bool(deep)
        if cfg["audit"]["_deep_xdevice"]:
            log("deep mode ON — every chain re-traced on the opposite device (2× evidence)")
        log(f"auditing {len(rows)} target(s) …")

        def prog(e: dict):
            job["done"] = e.get("index", job["done"])
            if e["event"] == "start":
                job["logs"].append(f"[{_time.perf_counter() - t0:7.1f}s] ▶ tracing {e['url'][:110]}")
            else:
                mark = "OK " if e.get("healthy") else "FAIL"
                job["logs"].append(f"[{_time.perf_counter() - t0:7.1f}s] [{mark} {e['index']}/{job['total']}] "
                                   f"{e['url'][:90]} · {e.get('ms')} ms · {e.get('reason')}")

        chains = await run_bulk_audit(rows, cfg, progress=prog)
        out = _bundle(chains, cfg)
        out["effective_settings"] = {"execution_mode": cfg.get("audit", {}).get("execution_mode"),
                                     "max_hops": cfg.get("audit", {}).get("max_hops"),
                                     "concurrency": cfg.get("audit", {}).get("concurrency")}
        job["result"] = out
        job["status"] = "done"
        log(f"complete — revenue at risk ${out['revenue_at_risk']:,.2f} · health {out['health_index']}/100")
    except Exception as e:  # noqa: BLE001
        job.update(status="error", error=str(e)[:500])
        log(f"ERROR {e}")
    job["elapsed"] = round(_time.perf_counter() - t0, 1)


@app.post("/audit/job")
async def start_job(body: FullAuditIn):
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"status": "running", "total": 0, "done": 0, "logs": [],
                 "t0": _time.perf_counter(), "result": None, "error": ""}
    asyncio.create_task(_run_job(jid, body.model_dump()))
    return {"job_id": jid}


@app.get("/audit/job/{jid}")
async def job_status(jid: str):
    job = JOBS.get(jid)
    if not job:
        return {"status": "unknown"}
    return {"status": job["status"], "total": job["total"], "done": job["done"],
            "elapsed": round(_time.perf_counter() - job["t0"], 1),
            "logs": job["logs"][-400:], "error": job["error"],
            "result": job["result"] if job["status"] == "done" else None}


# ---------------- docs (auto-updating) ----------------
@app.get("/content")
def content():
    return {"tool": "Outbound Affiliate Link Chain & Latency Tracker",
            "nav": NAV, "sections": SECTIONS}


# ---------------- real PDF ----------------
@app.post("/export/pdf")
async def export_pdf(body: dict):
    pdf = build_pdf(body.get("result", {}) or {}, body.get("input") or {})
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=affiliate-audit-report.pdf"})


class VerifyIn(BaseModel):
    sitemaps: list[str] = []
    urls: list[str] = []
    regex_ids: str = ""
    regex_sub: str = ""
    regex_promo: str = ""
    netmap: list[str] = []
    feeds: list[str] = []
    baselines: list[str] = []
    default_epc: float = 1.25


def _rx_ok(pattern: str) -> dict:
    try:
        re.compile(pattern or "")
        return {"ok": True, "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160]}


@app.post("/utils/verify-inputs")
async def verify_inputs(body: VerifyIn):
    """Verify every input field live — sitemaps, URLs, regexes, mappings, feeds, baselines."""
    out: dict = {}
    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"}) as client:
        out["sitemaps"] = []
        for sm in _clean_lines(body.sitemaps)[:20]:
            out["sitemaps"].append(await _check_sitemap(client, sm))

        async def _url(u: str) -> dict:
            async with sem:
                d = {"url": u, "live": False, "http": None, "final": "", "note": ""}
                try:
                    r = await client.head(u)
                    if r.status_code in (405, 501):
                        r = await client.get(u)
                    d.update(http=r.status_code, final=str(r.url),
                             live=r.status_code < 400,
                             note="LIVE" if r.status_code < 400 else f"HTTP {r.status_code} — NOT reachable")
                except Exception as e:  # noqa: BLE001
                    d["note"] = f"{type(e).__name__} — NOT reachable"
                return d
        out["urls"] = await asyncio.gather(*[_url(u) for u in _clean_lines(body.urls)[:80]])

        async def _feed(f: str) -> dict:
            async with sem:
                try:
                    r = await client.get(f)
                    return {"url": f, "live": r.status_code < 400, "http": r.status_code}
                except Exception:
                    return {"url": f, "live": False, "http": None}
        out["feeds"] = await asyncio.gather(*[_feed(f) for f in _clean_lines(body.feeds)[:20]])
    out["regex"] = {"ids": _rx_ok(body.regex_ids), "sub": _rx_ok(body.regex_sub),
                    "promo": _rx_ok(body.regex_promo)}
    parsed, bad = 0, []
    for ln in _clean_lines(body.netmap):
        if "=" in ln and ln.split("=")[1].strip():
            parsed += 1
        else:
            bad.append(ln[:80])
    out["netmap"] = {"ok": not bad, "parsed": parsed, "bad": bad[:10]}
    prow, bbad = 0, []
    for ln in _clean_lines(body.baselines):
        try:
            _, ctr, ctd = [x.strip() for x in ln.split(",")]
            float(ctr)
            float(ctd)
            prow += 1
        except Exception:
            bbad.append(ln[:80])
    out["baselines"] = {"ok": not bbad, "parsed": prow, "bad": bbad[:10]}
    out["epc_ok"] = (body.default_epc or 0) > 0
    return out


# ---------------- existing endpoints ----------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/app/")


@app.get("/health")
def health():
    return {"status": "ok", "service": "outbound-affiliate-link-chain-latency-tracker", "version": "2.0.0"}


@app.get("/config/defaults")
def config_defaults():
    return load_config(CFG_PATH)


@app.get("/utils/sitemap")
async def expand_sitemap(url: str = Query(...), limit: int = 100):
    urls = await fetch_sitemap_urls(url, limit=limit)
    return {"sitemap": url, "count": len(urls), "urls": urls}


@app.post("/utils/autofill")
async def autofill_from_site(body: AutofillIn):
    return await profile_site(body.site_url, max_pages=max(1, min(body.max_pages, 500)))


@app.post("/audit")
async def audit_one(body: SingleAuditIn):
    cfg = load_config(CFG_PATH)
    chain = await audit_single_url(body.model_dump(), cfg)
    return chain.model_dump()


@app.post("/audit/bulk")
async def audit_bulk(rows: list[SingleAuditIn]):
    cfg = load_config(CFG_PATH)
    chains = await run_bulk_audit([r.model_dump() for r in rows], cfg)
    return _bundle(chains, cfg)


@app.post("/audit/full")
async def audit_full(body: FullAuditIn):
    cfg = _deep_merge(load_config(CFG_PATH), body.settings or {})
    rows = await _expand_rows(body, cfg)
    if not rows:
        return {"error": "no targets — add URLs, paste a list, upload CSV or give a sitemap"}
    deep = cfg.get("audit", {}).get("deep_cross_device", "auto")
    cfg["audit"]["_deep_xdevice"] = (len(rows) <= 12) if deep == "auto" else bool(deep)
    chains = await run_bulk_audit(rows, cfg)
    out = _bundle(chains, cfg)
    out["effective_settings"] = {"execution_mode": cfg.get("audit", {}).get("execution_mode"),
                                 "max_hops": cfg.get("audit", {}).get("max_hops"),
                                 "concurrency": cfg.get("audit", {}).get("concurrency")}
    return out
