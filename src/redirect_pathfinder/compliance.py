"""Compliance & license guardian — RG text, badges, soft-404 NLP, geo-mismatch."""
from __future__ import annotations
import re
from urllib.parse import urlparse
from .models import ChainResult, ComplianceResult

TLD_GEO = {".co.uk": "UK", ".uk": "UK", ".us": "US", ".nj": "US-NJ", ".ca": "CA", ".com": "INTL", ".com.mx": "MX"}


def detect_soft404(html: str, status: int | None, phrases: list[str]) -> tuple[bool, list[str]]:
    low = (html or "").lower()
    hits = [p for p in phrases if p.lower() in low]
    # title-based 404 on a 200 is the classic iGaming expired-offer soft-404
    m_title = re.search(r"<title[^>]*>(.*?)</title>", low, re.DOTALL)
    title = m_title.group(1) if m_title else ""
    if "not found" in title or "404" in title:
        if "404" not in hits:
            hits.append("title:404/not-found")
    return (len(hits) > 0 and (status == 200 or status is None)), hits


def geo_mismatch_check(final_url: str, expected_geo: str) -> tuple[bool, str]:
    host = (urlparse(final_url).hostname or "").lower()
    detail = ""
    mismatch = False
    eg = expected_geo.upper()
    if eg == "UK" and not (host.endswith(".co.uk") or host.endswith(".uk") or "uk" in host or "begambleaware" in (final_url.lower())):
        # .com offshore is allowed ONLY if operator holds UKGC and page shows UKGC — flag as review-needed, not hard fail
        if host.endswith(".com") and "uk" not in host:
            mismatch = True
            detail = f"UK click landed on offshore host {host} — verify UKGC-licensed lander"
    if eg == "US-NJ" and ("uk" in host or host.endswith(".co.uk")):
        mismatch = True
        detail = f"NJ click landed on UK host {host}"
    if eg == "CA-ON" and host.endswith(".co.uk"):
        mismatch = True
        detail = f"Ontario click landed on UK host {host}"
    return mismatch, detail


def audit_compliance(chain: ChainResult, html: str, compliance_packs: dict, soft404_phrases: list[str]) -> ChainResult:
    pack = compliance_packs.get(chain.geo, {}) or {}
    required = pack.get("required_strings", [])
    badges = pack.get("license_badges", [])
    low = (html or "").lower()
    req_map = {s: (s.lower() in low) for s in required}
    badge_map = {b: (b.lower() in low) for b in badges}
    soft, hits = detect_soft404(html, chain.final_status, soft404_phrases)
    mm, detail = geo_mismatch_check(chain.final_url, chain.geo)
    compliant = all(req_map.values()) and not soft and not mm
    # empty page (blocked/empty) -> cannot verify -> non-compliant with explanation
    if not html:
        compliant = False
    chain.compliance = ComplianceResult(geo=chain.geo, required_strings=req_map,
                                        license_badges=badge_map, soft404_detected=soft,
                                        soft404_phrases_hit=hits, geo_mismatch=mm,
                                        geo_mismatch_detail=detail, compliant=compliant)
    if soft:
        chain.chain_ok = False
        chain.broken_reason = f"soft-404/expired-offer: {', '.join(hits[:3])}"
    return chain
