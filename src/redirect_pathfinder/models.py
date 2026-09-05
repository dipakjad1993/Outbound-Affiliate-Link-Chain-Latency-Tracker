"""Pydantic data models — strict contracts for every hop, chain and finding."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class Hop(BaseModel):
    index: int
    url: str
    status: Optional[int] = None
    location_header: Optional[str] = None
    ip: Optional[str] = None
    server: Optional[str] = None
    dns_ms: float = 0.0
    tcp_tls_ms: float = 0.0
    ttfb_ms: float = 0.0
    download_ms: float = 0.0
    total_ms: float = 0.0
    redirect_type: str = "unknown"   # 301/302/303/307/308/js-meta/js-window/final-200/error
    params: dict = Field(default_factory=dict)
    error: Optional[str] = None


class ParamEvent(BaseModel):
    param: str
    first_seen_hop: int
    last_seen_hop: Optional[int] = None
    stripped_at_hop: Optional[int] = None
    mutated_to: Optional[str] = None
    status: str  # survived | stripped | mutated


class ComplianceResult(BaseModel):
    geo: str = "unknown"
    required_strings: dict = Field(default_factory=dict)  # string -> found bool
    license_badges: dict = Field(default_factory=dict)
    soft404_detected: bool = False
    soft404_phrases_hit: list = Field(default_factory=list)
    geo_mismatch: bool = False
    geo_mismatch_detail: str = ""
    compliant: bool = True


class LatencyScore(BaseModel):
    total_ms: float = 0.0
    per_hop_ms: list = Field(default_factory=list)
    dns_total_ms: float = 0.0
    ttfb_total_ms: float = 0.0
    verdict: str = "fast"  # fast | warn | critical
    dropoff_risk_pct: float = 0.0


class ChainResult(BaseModel):
    source_url: str
    anchor_text: str = ""
    expected_operator: str = ""
    expected_network: str = ""
    geo: str = "US-NJ"
    device: str = "desktop_chrome"
    clicks_30d: float = 0.0
    epc: float = 1.25
    bonus_text_on_site: str = ""
    hops: list[Hop] = Field(default_factory=list)
    final_url: str = ""
    final_status: Optional[int] = None
    chain_ok: bool = True
    broken_reason: str = ""
    param_events: list[ParamEvent] = Field(default_factory=list)
    params_intact: bool = True
    compliance: ComplianceResult = Field(default_factory=ComplianceResult)
    latency: LatencyScore = Field(default_factory=LatencyScore)
    js_redirect_detected: bool = False
    needs_headless: bool = False
    headless_used: bool = False
    adblock_vulnerable: bool = False
    adblock_blocked_hosts: list = Field(default_factory=list)
    deeplink: dict = Field(default_factory=dict)
    brand_alignment: dict = Field(default_factory=dict)
    offer_discrepancy: dict = Field(default_factory=dict)
    network_path_ok: bool = True
    revenue_at_risk: float = 0.0
    health_score: float = 100.0
    revenue_verified: bool = False
    revenue_basis: str = ""
    page_forensics: dict = Field(default_factory=dict)
    device_parity: dict = Field(default_factory=dict)
    evidence: dict = Field(default_factory=dict)
    edge_patch: dict = Field(default_factory=dict)
    vendor_ticket: str = ""


class AuditFinding(BaseModel):
    severity: str  # critical | high | medium | low | info
    category: str
    message: str
    source_url: str = ""
