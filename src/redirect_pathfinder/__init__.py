"""iGaming Redirect-Pathfinder — enterprise affiliate chain & latency tracker."""
from .models import Hop, ChainResult, AuditFinding
from .orchestrator import run_bulk_audit, audit_single_url

__all__ = ["Hop", "ChainResult", "AuditFinding", "run_bulk_audit", "audit_single_url"]
__version__ = "1.0.0"
