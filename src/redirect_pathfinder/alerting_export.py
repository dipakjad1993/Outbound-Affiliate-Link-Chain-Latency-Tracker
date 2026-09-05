"""Alerting (Slack/Teams/webhook/email payload builders) + CSV/JSON/JIRA exporters."""
from __future__ import annotations
import csv
import json


def build_alerts(chain, high_value_clicks: int = 5000) -> list[dict]:
    alerts = []
    critical = (not chain.chain_ok) or (not chain.params_intact)
    if critical and float(chain.clicks_30d or 0) >= high_value_clicks:
        alerts.append({"channel": "slack+teams+email", "severity": "critical",
                       "text": f"CRITICAL: {chain.source_url} → {chain.final_url} broken ({chain.broken_reason or 'param-loss'}) | ${chain.revenue_at_risk:,.2f} at risk"})
    elif critical:
        alerts.append({"channel": "slack", "severity": "high",
                       "text": f"Broken affiliate chain: {chain.source_url} → {chain.final_url} | {chain.broken_reason or 'param-loss'}"})
    if chain.latency.verdict == "critical":
        alerts.append({"channel": "slack", "severity": "medium",
                       "text": f"Latency CRITICAL ({chain.latency.total_ms:.0f} ms) on {chain.source_url} — drop-off risk {chain.latency.dropoff_risk_pct}%"})
    if not chain.compliance.compliant:
        alerts.append({"channel": "email+slack", "severity": "high",
                       "text": f"Compliance violation on {chain.final_url}: soft404={chain.compliance.soft404_detected} geo_mismatch={chain.compliance.geo_mismatch}"})
    return alerts


def export_csv(chains: list, path: str) -> str:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source_url", "anchor", "operator", "network", "geo", "device", "final_url", "final_status",
                    "hops", "total_ms", "verdict", "params_intact", "chain_ok", "broken_reason",
                    "compliant", "revenue_at_risk", "health_score"])
        for c in chains:
            w.writerow([c.source_url, c.anchor_text, c.expected_operator, c.expected_network, c.geo, c.device,
                        c.final_url, c.final_status, len(c.hops), round(c.latency.total_ms, 1), c.latency.verdict,
                        c.params_intact, c.chain_ok, c.broken_reason, c.compliance.compliant,
                        c.revenue_at_risk, c.health_score])
    return path


def export_json(chains: list, path: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([c.model_dump() for c in chains], f, indent=2)
    return path


def jira_tickets(chains: list) -> list[dict]:
    out = []
    for c in chains:
        if c.chain_ok and c.params_intact and c.compliance.compliant:
            continue
        out.append({"project": "AFF", "issuetype": "Bug",
                    "summary": f"[Affiliate] Broken chain: {c.source_url} ({c.broken_reason or 'param-loss'})",
                    "description": f"Source: {c.source_url}\nFinal: {c.final_url} [{c.final_status}]\n"
                                   f"Hops: {len(c.hops)} total {c.latency.total_ms:.0f}ms\n"
                                   f"Params: {[(e.param, e.status) for e in c.param_events]}\n"
                                   f"Revenue at risk: ${c.revenue_at_risk}\nVendor ticket:\n{c.vendor_ticket}",
                    "priority": "Highest" if c.revenue_at_risk > 1000 else "High"})
    return out
