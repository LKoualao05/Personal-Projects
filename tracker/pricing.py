"""Per-token prices for cost tracking (USD per 1M tokens, standard online tier).

Source: Google's published Gemini pricing (aistudio.google.com/docs/pricing),
checked 2026-11-11. ``gemini-3.5-flash`` comes from third-party price trackers
because the official page was truncated. Vertex AI list prices normally match
these; check https://cloud.google.com/vertex-ai/generative-ai/pricing before
relying on the numbers for budgeting.

Prices can change on a date (introductory pricing), so each model has a list
of ``(effective_from, Price)`` entries; the latest entry on or before the
request date applies. Thinking tokens are billed at the output rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

PRICING_AS_OF = "2026-11-11"


@dataclass(frozen=True)
class Price:
    input: float
    output: float = 0.0
    cached_input: Optional[float] = None


PRICES: dict[str, list[tuple[date, Price]]] = {
    "gemini-4-argon": [
        (date(2026, 10, 13), Price(2.00, 10.00, 0.10)),
        (date(2026, 12, 1), Price(4.00, 20.00, 0.20)),
    ],
    "gemini-3.8-flash": [
        (date(2026, 9, 2), Price(0.75, 3.75, 0.075)),
        (date(2027, 1, 1), Price(1.50, 7.50, 0.15)),
    ],
    "gemini-3.5-flash": [(date(2026, 5, 19), Price(1.50, 9.00, 0.15))],
    "gemini-3.5-flash-lite": [(date(2026, 7, 21), Price(0.30, 2.50, 0.03))],
    "gemini-3.1-flash-lite": [(date(2026, 5, 7), Price(0.25, 1.50, 0.025))],
    "gemini-embedding-001": [(date(2025, 5, 20), Price(0.15))],
    "gemini-embedding-2": [(date(2026, 4, 22), Price(0.20))],
    "heuristic": [(date(2000, 1, 1), Price(0.0))],
    "hashing": [(date(2000, 1, 1), Price(0.0))],
}


def price_for(model: str, on: Optional[date] = None) -> Optional[Price]:
    on = on or date.today()
    entries = PRICES.get(model) or PRICES.get(model.split("@")[0])
    if not entries:
        return None
    applicable = [p for start, p in sorted(entries, key=lambda e: e[0]) if start <= on]
    return applicable[-1] if applicable else entries[0][1]


def cost_usd(model: str, input_tokens: int = 0, output_tokens: int = 0, thinking_tokens: int = 0,
             cached_tokens: int = 0, on: Optional[date] = None) -> Optional[float]:
    """Cost of one request; None if the model has no known price.

    ``input_tokens`` is Gemini's prompt_token_count, which already includes
    cached tokens, so those are billed at the cached rate instead.
    """
    p = price_for(model, on)
    if p is None:
        return None
    cached = min(cached_tokens, input_tokens)
    cached_rate = p.cached_input if p.cached_input is not None else p.input
    return ((input_tokens - cached) * p.input + cached * cached_rate + (output_tokens + thinking_tokens) * p.output) / 1e6


def stats_cost_usd(stats, on: Optional[date] = None) -> Optional[float]:
    """Cost for a ``tracker.llm.CallStats``."""
    return cost_usd(stats.model, stats.input_tokens, stats.output_tokens, stats.thinking_tokens,
                    stats.cached_tokens, on)
