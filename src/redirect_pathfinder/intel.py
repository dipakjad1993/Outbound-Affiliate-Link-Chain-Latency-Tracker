"""Bot-mitigation & anti-evasion helpers + adblock / deeplink / brand / offer auditors."""
from __future__ import annotations
import random
import re
from urllib.parse import urlparse

EASYLIST_HOSTS = ["track.adnetwork.com", "track.", "doubleclick.net", "googlesyndication.com",
                  "adservice", "ads.", "tracking.", "affiliate.", "cellxpert", "incomeaccess"]
ADBLOCK_TRACKING_HINTS = ["track.", "/track", "affiliate", "click", "pixel", "beacon", "syndication", "doubleclick"]


def spoof_headers(device: str = "desktop_chrome") -> dict:
    """Realistic fingerprint-spoofed header set to survive Cloudflare/Datadome/Akamai."""
    from .tracer import DEVICE_UAS
    sec_ch = '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"' if "mobile" not in device else '"Chromium";v="126", "Android WebView";v="126"'
    return {
        "User-Agent": DEVICE_UAS.get(device, DEVICE_UAS["desktop_chrome"]),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice(["en-US,en;q=0.9", "en-GB,en;q=0.9", "en-CA,en;q=0.8"]),
        "Sec-Ch-Ua": sec_ch,
        "Sec-Ch-Ua-Mobile": "?1" if "mobile" in device else "?0",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }


def audit_adblock(chain) -> object:
    """Simulate uBlock/EasyList + Brave/Safari-ITP blocking of intermediate tracking hosts."""
    blocked = []
    for h in chain.hops:
        host = (urlparse(h.url).hostname or "").lower()
        path = h.url.lower()
        if any(t in host or t in path for t in ADBLOCK_TRACKING_HINTS):
            # heuristic: dedicated tracking subdomains are the ones adblock kills
            if host.startswith("track.") or "doubleclick" in host or "syndication" in host or "/track" in path:
                blocked.append(host or h.url)
    chain.adblock_blocked_hosts = sorted(set(blocked))
    chain.adblock_vulnerable = len(chain.adblock_blocked_hosts) > 0
    return chain


def inspect_deeplink(chain, html: str = "") -> object:
    """Detect app-store / deep-link routing quality on mobile chains."""
    blob = (" ".join(h.url for h in chain.hops) + " " + (html or "")[:20000]).lower()
    has_universal = "applinks" in blob or "apple-app-site-association" in blob or "intent://" in blob
    has_store = "apps.apple.com" in blob or "play.google.com" in blob
    has_scheme = bool(re.search(r"(sportsbook|casino|betmgm|draftkings|bet365)[a-z]*://", blob))
    if "mobile" not in chain.device:
        verdict = "n/a-desktop"
    elif has_scheme or has_universal:
        verdict = "deep-link-ok"
    elif has_store:
        verdict = "falls-back-to-store"
    else:
        verdict = "web-only-no-deeplink"
    chain.deeplink = {"verdict": verdict, "has_app_scheme": has_scheme,
                      "has_store_fallback": has_store, "has_universal_link": has_universal}
    return chain


def brand_alignment(chain, html: str = "") -> object:
    """Ensure review-page brand matches final lander brand (broken operator redirect guard)."""
    expected = (chain.expected_operator or "").lower()
    text = ((html or "")[:30000]).lower() + " " + chain.final_url.lower()
    found = bool(expected) and expected.replace(" ", "") in text.replace(" ", "")
    # detect wrong-brand: any known rival brand on lander while expected missing
    rivals = ["bet365", "draftkings", "fanduel", "betmgm", "caesars", "williamhill", "unibet", "888"]
    rivals_hit = [r for r in rivals if r in text.replace(" ", "") and r != expected.replace(" ", "")]
    chain.brand_alignment = {"expected_brand": chain.expected_operator, "brand_found_on_lander": found,
                             "rival_brands_detected": rivals_hit,
                             "aligned": found and not rivals_hit if expected else True}
    return chain


BONUS_RX = re.compile(r"(deposit|bet|wager)\s*\$?\s*(\d+)[^$]{0,30}?get\s*\$?\s*(\d+)", re.IGNORECASE)


def _parse_bonus(s: str) -> tuple:
    m = BONUS_RX.search(s or "")
    return (m.group(2), m.group(3)) if m else (None, None)


def offer_discrepancy(chain) -> object:
    """Compare on-site bonus pitch vs lander reality — trust + ASA compliance guard."""
    # lander bonus extraction happens in orchestrator where HTML exists; here compare if provided
    site = _parse_bonus(chain.bonus_text_on_site or "")
    lander_text = chain.offer_discrepancy.get("lander_bonus_text", "") if isinstance(chain.offer_discrepancy, dict) else ""
    lander = _parse_bonus(lander_text)
    if not site or not lander:
        chain.offer_discrepancy = {**(chain.offer_discrepancy or {}), "comparable": False,
                                   "mismatch": False, "detail": "insufficient bonus text to compare"}
    else:
        mismatch = site != lander
        chain.offer_discrepancy = {**(chain.offer_discrepancy or {}), "comparable": True, "mismatch": mismatch,
                                   "site_offer": f"{site[0]}→{site[1]}", "lander_offer": f"{lander[0]}→{lander[1]}",
                                   "detail": "MATCH" if not mismatch else f"Site {site} ≠ Lander {lander}"}
    return chain


def extract_lander_bonus(html: str) -> str:
    m = BONUS_RX.search(html or "")
    return m.group(0) if m else ""
