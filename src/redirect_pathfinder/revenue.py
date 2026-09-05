"""Revenue protection math + network SLA matrix."""
from __future__ import annotations
from collections import defaultdict


def score_revenue_and_health(chain) -> object:
    clicks = float(chain.clicks_30d or 0)
    epc = float(chain.epc or 0)
    fatal = (not chain.chain_ok) or (not chain.params_intact) or (chain.final_status and chain.final_status >= 400)
    degraded = (chain.latency.verdict == "critical") or (not chain.compliance.compliant) or (not chain.network_path_ok)
    if fatal:
        chain.revenue_at_risk = round(clicks * epc, 2)
    elif degraded:
        chain.revenue_at_risk = round(clicks * epc * 0.35, 2)
    else:
        chain.revenue_at_risk = 0.0
    # health 0-100
    score = 100.0
    if not chain.chain_ok:
        score -= 45
    if not chain.params_intact:
        score -= 30
    if chain.final_status and chain.final_status >= 400:
        score -= 20
    if chain.latency.verdict == "critical":
        score -= 15
    elif chain.latency.verdict == "warn":
        score -= 7
    if not chain.compliance.compliant:
        score -= 12
    if not chain.network_path_ok:
        score -= 10
    if chain.adblock_vulnerable:
        score -= 5
    chain.health_score = round(max(0.0, min(100.0, score)), 1)
    chain.revenue_basis = ("verified — user-supplied GA4 clicks + EPC"
                           if chain.revenue_verified else
                           "estimated — reach-scaled clicks × table EPC "
                           "(connect GA4 / affiliate API for verified $)")
    return chain


def network_sla_matrix(chains: list) -> list[dict]:
    by_net: dict[str, list] = defaultdict(list)
    for c in chains:
        by_net[c.expected_network or "unknown"].append(c)
    rows = []
    for net, items in by_net.items():
        lat = [c.latency.total_ms for c in items if c.latency.total_ms]
        ok = sum(1 for c in items if c.chain_ok and c.params_intact)
        rows.append({"network": net, "checks": len(items),
                     "success_rate_pct": round(ok / max(1, len(items)) * 100, 1),
                     "avg_latency_ms": round(sum(lat) / max(1, len(lat)), 1) if lat else 0,
                     "revenue_at_risk": round(sum(c.revenue_at_risk for c in items), 2)})
    return sorted(rows, key=lambda r: r["avg_latency_ms"])
