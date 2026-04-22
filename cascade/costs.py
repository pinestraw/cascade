"""Deterministic cost estimation for model-backed Cascade workflows.

All functions here are pure and model-free.  No API calls are made.
Costs are approximations — verify current provider pricing before use.
"""
from __future__ import annotations

from cascade.config import ModelProfile


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

# Default expected output token counts by task type (conservative estimates).
DEFAULT_EXPECTED_OUTPUT_TOKENS: dict[str, int] = {
    "plan": 5000,
    "implement": 30000,
    "diagnose": 8000,
    "fix": 12000,
    "review": 8000,
    "summarize": 3000,
}


def estimate_tokens(text: str) -> int:
    """Rough token count estimate.

    Uses the simple approximation: max(1, len(text) // 4).
    Suitable for budget guarding; not accurate enough for billing.
    """
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    model_profile: ModelProfile,
) -> float:
    """Return estimated USD cost for a single model call.

    Formula:
        input_tokens / 1_000_000 * input_cost_per_million
      + output_tokens / 1_000_000 * output_cost_per_million

    Args:
        input_tokens: Estimated number of input tokens.
        output_tokens: Expected number of output tokens.
        model_profile: The resolved model profile with cost fields.

    Returns:
        Estimated cost in USD (float).
    """
    input_cost = (input_tokens / 1_000_000) * model_profile.input_cost_per_million
    output_cost = (output_tokens / 1_000_000) * model_profile.output_cost_per_million
    return input_cost + output_cost


def format_cost(cost_usd: float) -> str:
    """Format cost in a human-readable way."""
    if cost_usd < 0.001:
        return f"~${cost_usd * 1000:.3f} mUSD"
    return f"~${cost_usd:.4f} USD"


def cost_summary_lines(
    input_tokens: int,
    output_tokens: int,
    model_profile: ModelProfile,
    profile_name: str,
) -> list[str]:
    """Return a list of human-readable cost summary lines."""
    from cascade.config import model_id_for_opencode  # avoid circular at module top

    cost = estimate_cost(input_tokens, output_tokens, model_profile)
    model_id = model_id_for_opencode(model_profile)
    return [
        f"Profile         : {profile_name}",
        f"Model           : {model_id}",
        f"Input tokens    : ~{input_tokens:,}",
        f"Output tokens   : ~{output_tokens:,}",
        f"Estimated cost  : {format_cost(cost)}",
        "Note: Costs are approximations. Verify current pricing at "
        "https://openrouter.ai/models before billing decisions.",
    ]
