"""Regex rule engine — validates affiliate parameter survival + network routing."""
from __future__ import annotations
import re
from urllib.parse import urlparse, parse_qsl
from .models import ChainResult, ParamEvent


def extract_params(url: str) -> dict:
    try:
        return dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
    except Exception:
        return {}


def compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def audit_param_survival(chain: ChainResult, required_patterns: list[str]) -> ChainResult:
    """Hop-by-hop mutation audit. Flags silent stripping + key mutation."""
    compiled = compile_patterns(required_patterns)
    if not chain.hops:
        chain.params_intact = False
        return chain
    # collect all param keys seen at hop 0 that match required patterns
    hop_params = [extract_params(h.url) for h in chain.hops]
    for i, h in enumerate(chain.hops):
        h.params = hop_params[i]
    initial = hop_params[0] if hop_params else {}
    tracked: dict[str, ParamEvent] = {}
    for k, v in initial.items():
        if any(rx.search(k) for rx in compiled):
            tracked[k.lower()] = ParamEvent(param=k, first_seen_hop=0, last_seen_hop=0, status="survived")

    for idx in range(1, len(hop_params)):
        current_keys = {k.lower(): k for k in hop_params[idx].keys()}
        for low_key, ev in tracked.items():
            if ev.status != "survived":
                continue
            if low_key in current_keys:
                ev.last_seen_hop = idx
                # value mutation check
                old_v = str(initial.get(ev.param, ""))
                new_v = str(hop_params[idx].get(current_keys[low_key], ""))
                if old_v and new_v and old_v != new_v:
                    ev.mutated_to = f"{current_keys[low_key]}={new_v} (was {old_v})"
                    ev.status = "mutated"
                    ev.last_seen_hop = idx
            else:
                # possible key rename? e.g. btag -> tag
                ev.stripped_at_hop = idx
                ev.last_seen_hop = idx - 1
                ev.status = "stripped"
    chain.param_events = list(tracked.values())
    chain.params_intact = all(e.status == "survived" for e in chain.param_events)
    # If no tracked params at all but rules exist -> flag as missing-ids (common for plain review links)
    if not tracked and required_patterns:
        chain.params_intact = False
    return chain


def audit_network_path(chain: ChainResult, network_mappings: dict) -> ChainResult:
    """Verify expected affiliate network appears somewhere in the chain."""
    op = (chain.expected_operator or "").lower()
    if not op or op not in {k.lower(): v for k, v in network_mappings.items()}:
        chain.network_path_ok = True
        return chain
    lookup = {k.lower(): v for k, v in network_mappings.items()}
    expected_domains = [d.lower() for d in lookup.get(op, [])]
    chain_urls = " ".join(h.url.lower() for h in chain.hops) + " " + chain.final_url.lower()
    chain.network_path_ok = any(d in chain_urls for d in expected_domains)
    return chain
