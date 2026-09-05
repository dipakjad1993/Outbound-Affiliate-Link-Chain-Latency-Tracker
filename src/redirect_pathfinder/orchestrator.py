"""Orchestrator — async bulk audit engine wiring all 10 core features."""
from __future__ import annotations
import asyncio
import yaml
from .models import ChainResult
from .tracer import trace_chain, try_headless_follow
from .rule_engine import audit_param_survival, audit_network_path
from .latency import score_latency
from .compliance import audit_compliance
from .intel import spoof_headers, audit_adblock, inspect_deeplink, brand_alignment, offer_discrepancy, extract_lander_bonus
from .revenue import score_revenue_and_health
from .edge_healer import build_edge_patch, build_vendor_ticket


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _to_float(v, default=0.0) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "") or default)
    except Exception:
        return default


def page_forensics(html: str, url: str) -> dict:
    """Deep final-lander forensics — real parsed measurements, not guesses."""
    if not html:
        return {"words": 0, "kb": 0.0, "title": "", "h1": "", "lang": "",
                "outlinks": 0, "images": 0, "note": "empty body (blocked or error)"}
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        h1 = soup.find("h1")
        return {"words": len(text.split()), "kb": round(len(html) / 1024, 1),
                "title": (soup.title.string.strip()[:160] if soup.title and soup.title.string else ""),
                "h1": (h1.get_text(" ", strip=True)[:140] if h1 else ""),
                "lang": (soup.html.get("lang", "") if soup.html else "") or "",
                "outlinks": len(soup.find_all("a", href=True)),
                "images": len(soup.find_all("img"))}
    except Exception:
        return {"words": 0, "kb": round(len(html or "") / 1024, 1), "title": "",
                "h1": "", "lang": "", "outlinks": 0, "images": 0}


async def audit_single_url(row: dict, cfg: dict) -> ChainResult:
    audit_cfg = cfg.get("audit", {})
    max_hops = int(audit_cfg.get("max_hops", 12))
    timeout_s = int(audit_cfg.get("timeout_seconds", 20))
    mode = audit_cfg.get("execution_mode", "hybrid")
    src = row.get("source_url") or row.get("url") or ""
    device = row.get("device", "desktop_chrome") or "desktop_chrome"
    chain = ChainResult(source_url=src, anchor_text=row.get("anchor_text", ""),
                        expected_operator=row.get("expected_operator", ""),
                        expected_network=row.get("expected_network", ""),
                        geo=row.get("geo", "US-NJ") or "US-NJ", device=device,
                        clicks_30d=_to_float(row.get("clicks_30d", 0)),
                        epc=_to_float(row.get("epc", cfg.get("revenue", {}).get("default_epc", 1.25))),
                        bonus_text_on_site=row.get("bonus_text_on_site", ""))
    headers = spoof_headers(device)
    try:
        hops, html, js_hit, needs_headless = await trace_chain(src, device=device, max_hops=max_hops,
                                                               timeout_s=timeout_s, extra_headers=headers)
    except Exception as e:  # noqa: BLE001 — never let one URL kill a bulk run
        from .models import Hop
        hops, html, js_hit, needs_headless = ([Hop(index=0, url=src, redirect_type="error", error=str(e)[:250])], "", False, False)
    chain.hops = hops
    chain.js_redirect_detected = js_hit
    chain.needs_headless = needs_headless
    # headless escalation
    if mode in ("hybrid", "headless_only") and (needs_headless or (mode == "headless_only" and hops)):
        try:
            furl, hhtml = await try_headless_follow(hops[-1].url if hops else src, device=device)
            if hhtml:
                chain.headless_used = True
                html = hhtml
                if furl and furl != (hops[-1].url if hops else src):
                    from .models import Hop as H
                    hops.append(H(index=len(hops), url=furl, status=200, redirect_type="headless-final"))
                    chain.hops = hops
        except Exception:
            pass
    chain.final_url = chain.hops[-1].url if chain.hops else src
    chain.final_status = chain.hops[-1].status if chain.hops else None
    chain.revenue_verified = bool(row.get("revenue_verified", False))
    chain.page_forensics = page_forensics(html, chain.final_url)
    chain.evidence = {"http_requests": len(chain.hops),
                      "dns_lookups": sum(1 for h in chain.hops if h.ip),
                      "bytes_downloaded": len(html or ""),
                      "execution_mode": mode,
                      "headless_used": chain.headless_used}
    # broken-chain verdict
    last = chain.hops[-1] if chain.hops else None
    if not chain.hops:
        chain.chain_ok, chain.broken_reason = False, "no-hops"
    elif last and last.error and "loop" in last.error:
        chain.chain_ok, chain.broken_reason = False, "redirect-loop"
    elif last and last.error:
        chain.chain_ok, chain.broken_reason = False, last.error[:200]
    elif last and last.status and last.status >= 400:
        chain.chain_ok, chain.broken_reason = False, f"final-http-{last.status}"
    else:
        chain.chain_ok, chain.broken_reason = True, ""
    # rule engines
    tr = cfg.get("tracking_rules", {})
    chain = audit_param_survival(chain, tr.get("required_param_patterns", []))
    chain = audit_network_path(chain, tr.get("network_mappings", {}))
    chain = score_latency(chain, int(cfg.get("latency", {}).get("warn_threshold_ms", 900)),
                          int(cfg.get("latency", {}).get("drop_off_threshold_ms", 1800)))
    chain.offer_discrepancy = {"lander_bonus_text": extract_lander_bonus(html or "")}
    chain = offer_discrepancy(chain)
    chain = audit_compliance(chain, html or "", cfg.get("compliance_packs", {}), cfg.get("soft404_phrases", []))
    chain = audit_adblock(chain)
    chain = inspect_deeplink(chain, html or "")
    chain = brand_alignment(chain, html or "")
    chain = score_revenue_and_health(chain)
    build_edge_patch(chain)
    build_vendor_ticket(chain)
    # Deep cross-device re-trace: same chain on the opposite device class.
    # Real 2× HTTP work — proves the chain behaves identically for mobile/desktop.
    if cfg.get("audit", {}).get("_deep_xdevice"):
        alt = "mobile_ios" if device.startswith("desktop") else "desktop_chrome"
        try:
            hops2, _, _, _ = await trace_chain(src, device=alt, max_hops=max_hops,
                                               timeout_s=timeout_s, extra_headers=spoof_headers(alt))
            fin2 = hops2[-1].url if hops2 else ""
            from urllib.parse import urlparse as _up, parse_qsl as _pqs
            q1 = dict(_pqs(_up(chain.final_url).query))
            q2 = dict(_pqs(_up(fin2).query))
            tracked = [e.param for e in chain.param_events]
            kept1 = [k for k in q1 if any(k.lower() == t.lower() for t in tracked)]
            kept2 = [k for k in q2 if any(k.lower() == t.lower() for t in tracked)]
            chain.device_parity = {"primary_device": device, "alt_device": alt,
                                   "alt_final_url": fin2,
                                   "alt_status": hops2[-1].status if hops2 else None,
                                   "same_final": fin2.split("?")[0] == chain.final_url.split("?")[0],
                                   "params_survive_on_alt": kept2,
                                   "parity_ok": (fin2.split("?")[0] == chain.final_url.split("?")[0]
                                                 and set(k.lower() for k in kept1) == set(k.lower() for k in kept2))}
            chain.evidence["http_requests"] += len(hops2)
        except Exception as e:  # noqa: BLE001
            chain.device_parity = {"parity_ok": False, "note": f"alt-device trace failed: {str(e)[:150]}"}
    return chain


async def run_bulk_audit(rows: list[dict], cfg: dict, progress=None) -> list[ChainResult]:
    """progress(event) is called with {'event': 'start'|'done', 'url', 'index', ...} for live logs."""
    sem = asyncio.Semaphore(int(cfg.get("audit", {}).get("concurrency", 8)))
    state = {"done": 0}

    async def _one(r: dict) -> ChainResult:
        async with sem:
            if progress:
                progress({"event": "start", "url": r.get("source_url", "")})
            c = await audit_single_url(r, cfg)
            state["done"] += 1
            if progress:
                progress({"event": "done", "url": c.source_url, "index": state["done"],
                          "healthy": bool(c.chain_ok and c.params_intact),
                          "ms": round(c.latency.total_ms, 1),
                          "reason": c.broken_reason or "ok"})
            return c

    return list(await asyncio.gather(*[_one(r) for r in rows]))
