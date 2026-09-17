"""Paywall handling (ability 23).

The agent brings the website to the client, states plainly that there is a
paywall, says what that purchase unlocks *for this specific strategy* and what
the free fallback costs in accuracy. The client pays. Work continues. The
credential is stored and never asked for again.

The agent never pays, never enters a card, and never decides for the client.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ee_agent.data.source_registry import CAPABILITIES
from ee_agent.secrets.vault import has_secret, vault


@dataclass
class FallbackCost:
    """What the free path actually costs in accuracy, stated concretely."""

    source: str
    resolution_loss: str = ""
    history_loss: str = ""
    flow_loss: str = ""
    reliability_loss: str = ""

    def lines(self) -> list[str]:
        return [x for x in (self.resolution_loss, self.history_loss, self.flow_loss, self.reliability_loss) if x]


@dataclass
class PaywallNotice:
    blocked_source: str
    url: str
    unlocks: list[str]
    fallback: FallbackCost | None
    secret_name: str
    monthly_usd: float | None = None
    notes: list[str] = field(default_factory=list)

    def message(self) -> str:
        lines = [
            f"There is a paywall at {self.blocked_source}.",
            f"  Open: {self.url}",
            "  What buying it unlocks for THIS strategy:",
        ]
        lines += [f"    - {u}" for u in self.unlocks]
        if self.fallback:
            lines.append(f"  If you don't buy it, I fall back to {self.fallback.source}, which costs:")
            lines += [f"    - {x}" for x in self.fallback.lines()] or ["    - nothing measurable"]
        else:
            lines.append("  There is no free fallback for this data. Without it, this part stays unbuilt.")
        if self.monthly_usd:
            lines.append(f"  Vendor price: about ${self.monthly_usd:,.2f}/month, paid by you directly.")
        lines.append(
            f"  When you have it, run: ee-agent secrets set {self.secret_name}  "
            "(stored in your OS keychain, never logged, never sent to any model, "
            f"removable with: ee-agent secrets delete {self.secret_name})"
        )
        lines.append("  I am carrying on with the free path in the meantime.")
        return "\n".join(lines)


def notice_for(source: str, spec=None, needed_for: str = "") -> PaywallNotice:
    cap = CAPABILITIES[source]
    unlocks: list[str] = []
    if cap.has_flow:
        unlocks.append("real order flow: cumulative delta, absorption, bid-ask imbalance, footprint")
    if "tick" in cap.resolutions:
        unlocks.append("tick data, which is what makes stop-run and queue-position modelling exact")
    unlocks.append(f"{cap.history_days // 365} years of history at {', '.join(cap.resolutions[:4])}")
    if needed_for:
        unlocks.append(needed_for)
    if spec is not None:
        flow_conds = [c.type for c in spec.all_conditions() if c.type in ("cum_delta_cross", "absorption")]
        if flow_conds:
            unlocks.append(
                f"your strategy uses {', '.join(sorted(set(flow_conds)))}, which runs on a "
                "bar-derived proxy without this data"
            )

    free = _best_free_alternative(cap)
    fallback = None
    if free:
        fallback = FallbackCost(
            source=free.name,
            resolution_loss=(
                f"lowest resolution becomes {free.resolutions[0]} instead of {cap.resolutions[0]}"
                if free.resolutions[0] != cap.resolutions[0]
                else ""
            ),
            history_loss=(
                f"history shortens from {cap.history_days} days to {free.history_days} days"
                if free.history_days < cap.history_days
                else ""
            ),
            flow_loss=(
                "order flow degrades to bar-derived proxies (sign of the close within the bar range)"
                if cap.has_flow and not free.has_flow
                else ""
            ),
            reliability_loss=(
                f"source reliability drops from {cap.reliability:.2f} to {free.reliability:.2f}"
                if free.reliability < cap.reliability
                else ""
            ),
        )

    return PaywallNotice(
        blocked_source=source,
        url=cap.paywall_url or "",
        unlocks=unlocks,
        fallback=fallback,
        secret_name=cap.requires_secret or "",
        monthly_usd=None,
    )


def _best_free_alternative(cap):
    candidates = [
        c
        for c in CAPABILITIES.values()
        if c.name != cap.name and c.keyless and set(c.asset_classes) & set(cap.asset_classes)
    ]
    return max(candidates, key=lambda c: (c.reliability, c.history_days), default=None)


def resolve_after_purchase(secret_name: str, value: str) -> str:
    """Store it once. Never ask again."""
    backend = vault().set(secret_name, value)
    return f"Stored {secret_name} in the {backend}. I will not ask for it again."


def is_paywalled(source: str) -> bool:
    cap = CAPABILITIES.get(source)
    return bool(cap and cap.requires_secret and not has_secret(cap.requires_secret))
