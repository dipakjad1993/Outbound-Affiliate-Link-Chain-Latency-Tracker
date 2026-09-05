"""Enterprise CLI — `python cli.py audit --csv samples/urls.csv` etc."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from redirect_pathfinder.orchestrator import load_config, run_bulk_audit
from redirect_pathfinder.input_ingest import load_csv_targets, fetch_sitemap_urls
from redirect_pathfinder.revenue import network_sla_matrix
from redirect_pathfinder.alerting_export import export_csv, export_json, jira_tickets, build_alerts


async def _run_audit(args) -> int:
    cfg = load_config(args.config)
    rows: list[dict] = []
    if args.csv:
        rows.extend(load_csv_targets(args.csv))
    if args.url:
        rows.append({"source_url": args.url, "anchor_text": args.anchor or "", "expected_operator": args.operator or "",
                     "expected_network": args.network or "", "geo": args.geo, "device": args.device,
                     "clicks_30d": args.clicks, "epc": args.epc, "bonus_text_on_site": args.bonus or ""})
    if args.sitemap:
        sm = await fetch_sitemap_urls(args.sitemap)
        for u in sm[: args.limit]:
            rows.append({"source_url": u, "geo": args.geo, "device": args.device, "clicks_30d": 500, "epc": 1.25})
    rows = rows[: args.limit]
    if not rows:
        print("No targets. Provide --csv, --url or --sitemap.", flush=True)
        return 2
    print(f"[pathfinder] auditing {len(rows)} target(s) …", flush=True)
    chains = await run_bulk_audit(rows, cfg)
    os.makedirs("output", exist_ok=True)
    export_json(chains, "output/audit_report.json")
    export_csv(chains, "output/audit_report.csv")
    with open("output/jira_tickets.json", "w", encoding="utf-8") as f:
        json.dump(jira_tickets(chains), f, indent=2)
    sla = network_sla_matrix(chains)
    with open("output/sla_matrix.json", "w", encoding="utf-8") as f:
        json.dump(sla, f, indent=2)
    alerts = []
    for c in chains:
        alerts.extend(build_alerts(c))
    with open("output/alerts.json", "w", encoding="utf-8") as f:
        json.dump(alerts, f, indent=2)
    total_risk = sum(c.revenue_at_risk for c in chains)
    health = sum(c.health_score for c in chains) / max(1, len(chains))
    broken = sum(1 for c in chains if not c.chain_ok or not c.params_intact)
    print("=" * 78)
    print(f"Targets audited : {len(chains)}")
    print(f"Broken/param-loss: {broken}")
    print(f"Revenue at risk : ${total_risk:,.2f}")
    print(f"Health index    : {health:.1f}/100")
    print(f"SLA matrix      : {sla}")
    print("Wrote: output/audit_report.json, output/audit_report.csv, output/jira_tickets.json, output/sla_matrix.json, output/alerts.json")
    print("Open dashboard/dashboard.html in a browser (it reads ../output/audit_report.json).")
    print("=" * 78)
    for c in chains:
        flag = "OK " if (c.chain_ok and c.params_intact) else "FAIL"
        print(f"[{flag}] {c.source_url} -> {c.final_url} [{c.final_status}] {c.latency.total_ms:.0f}ms rev-risk=${c.revenue_at_risk:.2f} {c.broken_reason}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="iGaming Redirect-Pathfinder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit", help="run chain audit")
    a.add_argument("--csv", default="samples/urls.csv")
    a.add_argument("--url", default="")
    a.add_argument("--sitemap", default="")
    a.add_argument("--config", default="config/enterprise.yaml")
    a.add_argument("--geo", default="US-NJ")
    a.add_argument("--device", default="desktop_chrome")
    a.add_argument("--operator", default="")
    a.add_argument("--network", default="")
    a.add_argument("--anchor", default="")
    a.add_argument("--bonus", default="")
    a.add_argument("--clicks", type=float, default=1000)
    a.add_argument("--epc", type=float, default=1.25)
    a.add_argument("--limit", type=int, default=100)
    s = sub.add_parser("serve", help="run FastAPI server")
    s.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()
    if args.cmd == "audit":
        raise SystemExit(asyncio.run(_run_audit(args)))
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("redirect_pathfinder.api:app", host="0.0.0.0", port=args.port, reload=False)


if __name__ == "__main__":
    main()
