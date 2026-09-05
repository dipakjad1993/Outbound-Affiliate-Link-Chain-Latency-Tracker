"""Multi-hop asynchronous tracing engine — real HTTP timing, DNS, redirect capture."""
from __future__ import annotations
import asyncio
import re
import socket
import time
from urllib.parse import urljoin, urlparse

import httpx

from .models import Hop

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
META_REFRESH_RX = re.compile(r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]*content=["\']?\s*\d+\s*;\s*url=(.*?)["\']?\s*/?>', re.IGNORECASE)
JS_REDIRECT_RX = re.compile(r'(window\.location(?:\.href|\.replace)?|location\.href|location\.replace)\s*\(?\s*=[(]?\s*["\']([^"\']+)["\']', re.IGNORECASE)

DEVICE_UAS = {
    "desktop_chrome": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "desktop_edge": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 Edg/126.0",
    "mobile_ios": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "mobile_android": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36",
}


async def resolve_dns(host: str) -> tuple[str, float]:
    t0 = time.perf_counter()
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ip = infos[0][4][0] if infos else ""
    except Exception:
        ip = ""
    return ip, (time.perf_counter() - t0) * 1000.0


def detect_client_redirects(html: str, base_url: str) -> tuple[str, str]:
    """Return (target, kind) for meta-refresh / JS redirects found in HTML."""
    m = META_REFRESH_RX.search(html or "")
    if m:
        return urljoin(base_url, m.group(1).strip().strip("'\"")), "js-meta"
    m2 = JS_REDIRECT_RX.search(html or "")
    if m2:
        try:
            return urljoin(base_url, m2.group(2).strip()), "js-window"
        except Exception:
            pass
    return "", ""


async def trace_chain(start_url: str, device: str = "desktop_chrome",
                      max_hops: int = 12, timeout_s: int = 20,
                      extra_headers: dict | None = None) -> tuple[list[Hop], str, bool, bool]:
    """Trace redirect chain WITHOUT auto-following. Returns (hops, final_html, js_detected, needs_headless)."""
    ua = DEVICE_UAS.get(device, DEVICE_UAS["desktop_chrome"])
    headers = {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9", "Upgrade-Insecure-Requests": "1"}
    if extra_headers:
        headers.update(extra_headers)
    hops: list[Hop] = []
    final_html = ""
    js_detected = False
    needs_headless = False
    url = start_url
    visited = set()

    timeout = httpx.Timeout(timeout_s, connect=10.0)
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, verify=True) as client:
        for i in range(max_hops):
            if url in visited:
                hops.append(Hop(index=i, url=url, redirect_type="error", error="redirect-loop"))
                break
            visited.add(url)
            host = urlparse(url).hostname or ""
            ip, dns_ms = await resolve_dns(host) if host else ("", 0.0)
            t_start = time.perf_counter()
            try:
                t_req = time.perf_counter()
                resp = await client.get(url, headers=headers)
                t_headers = time.perf_counter()
                body = b""
                try:
                    body = await resp.aread()
                except Exception:
                    body = resp.content or b""
                t_end = time.perf_counter()
                ttfb = (t_headers - t_req) * 1000.0
                download = (t_end - t_headers) * 1000.0
                total = (t_end - t_start) * 1000.0
                tcp_tls = max(0.0, total - dns_ms - download - (ttfb * 0.15))
                loc = resp.headers.get("location")
                server = resp.headers.get("server", "")
                if resp.status_code in REDIRECT_STATUSES and loc:
                    nxt = urljoin(url, loc)
                    hops.append(Hop(index=i, url=url, status=resp.status_code, location_header=loc,
                                    ip=ip, server=server, dns_ms=round(dns_ms, 1), tcp_tls_ms=round(tcp_tls, 1),
                                    ttfb_ms=round(ttfb, 1), download_ms=round(download, 1), total_ms=round(total, 1),
                                    redirect_type=str(resp.status_code)))
                    url = nxt
                    continue
                # final (non-redirect)
                html = body.decode("utf-8", errors="ignore") if body else ""
                final_html = html
                rtype = "final-200" if resp.status_code < 400 else f"final-{resp.status_code}"
                # client-side redirect detection on 200s
                if resp.status_code == 200 and len(html) > 0:
                    target, kind = detect_client_redirects(html[:60000], url)
                    if target and target not in visited:
                        js_detected = True
                        hops.append(Hop(index=i, url=url, status=resp.status_code, ip=ip, server=server,
                                        dns_ms=round(dns_ms, 1), tcp_tls_ms=round(tcp_tls, 1),
                                        ttfb_ms=round(ttfb, 1), download_ms=round(download, 1),
                                        total_ms=round(total, 1), redirect_type=kind,
                                        location_header=target))
                        if len(hops) < max_hops:
                            url = target
                            continue
                    # fingerprint / bot-wall heuristics
                    low = html[:8000].lower()
                    if any(s in low for s in ["just a moment", "attention required", "datadome", "captcha", "verify you are human", "access denied"]):
                        needs_headless = True
                hops.append(Hop(index=i, url=url, status=resp.status_code, ip=ip, server=server,
                                dns_ms=round(dns_ms, 1), tcp_tls_ms=round(tcp_tls, 1),
                                ttfb_ms=round(ttfb, 1), download_ms=round(download, 1),
                                total_ms=round(total, 1), redirect_type=rtype))
                url = url  # final
                break
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                total = (time.perf_counter() - t_start) * 1000.0
                hops.append(Hop(index=i, url=url, ip=ip, dns_ms=round(dns_ms, 1),
                                total_ms=round(total, 1), ttfb_ms=round(total, 1),
                                redirect_type="error", error=f"{type(e).__name__}: {str(e)[:220]}"))
                break
            except Exception as e:  # noqa: BLE001
                total = (time.perf_counter() - t_start) * 1000.0
                hops.append(Hop(index=i, url=url, ip=ip, dns_ms=round(dns_ms, 1),
                                total_ms=round(total, 1), redirect_type="error",
                                error=f"{type(e).__name__}: {str(e)[:220]}"))
                break
    return hops, final_html, js_detected, needs_headless


async def try_headless_follow(url: str, device: str = "desktop_chrome", timeout_s: int = 25) -> tuple[str, str]:
    """Best-effort Playwright escalation. Returns (final_url, html). Graceful no-op if Playwright missing."""
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception:
        return url, ""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx_args: dict = {}
            if device.startswith("mobile"):
                ctx_args = {"viewport": {"width": 390, "height": 844}, "is_mobile": True,
                            "user_agent": DEVICE_UAS.get(device)}
            else:
                ctx_args = {"user_agent": DEVICE_UAS.get(device)}
            ctx = await browser.new_context(**ctx_args)
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            await page.wait_for_timeout(2500)
            final = page.url
            html = await page.content()
            await browser.close()
            return final, html
    except Exception:
        return url, ""
