"""Model choices offered in Settings (fal `openrouter/router/vision`; any other OpenRouter id still works).

Prices: OpenRouter list prices, $ per 1M tokens (input, output), checked 2026-11-17. The rough cost of
one request is calibrated on real runs: an anchor with reasoning ≈ 9k input + 25k output tokens
(gemini-3.8-flash: ~$0.12), a followed key ≈ 11k input + 1k output (claude-haiku-5.5: ~$0.0016).
Quality on this task is NOT verified for every entry — compare them with tools/bench_vlm.py.
"""

from __future__ import annotations

ANCHOR_TOKENS = (9_000, 25_000)
FOLLOW_TOKENS = (11_000, 1_000)

# (id, short note, input $/1M, output $/1M)
ANCHOR_MODELS = [
    ("google/gemini-3.8-flash", "базова, перевірена", 0.825, 4.125),
    ("google/gemini-4-hydrogen", "Gemini 4, дешевший", 1.5, 7.5),
    ("google/gemini-4-carbon", "Gemini 4, найновіший", 2.0, 10.0),
    ("google/gemini-4-argon", "Gemini 4", 2.0, 10.0),
    ("openai/gpt-6.1-sol", "флагман OpenAI", 2.0, 10.0),
    ("openai/gpt-6.1-terra", "OpenAI, дешевший", 1.0, 5.0),
    ("anthropic/claude-sonnet-5.5", "Anthropic", 2.0, 10.0),
    ("anthropic/claude-opus-5.5", "Anthropic, найсильніша", 4.0, 20.0),
    ("qwen/qwen4-max", "Qwen 4", 1.425, 4.275),
    ("x-ai/grok-4.7", "xAI", 2.0, 6.0),
]

FOLLOW_MODELS = [
    ("anthropic/claude-haiku-5.5", "базова, перевірена", 0.1, 0.5),
    ("openai/gpt-6-luna", "OpenAI, швидка", 0.1, 0.5),
    ("qwen/qwen3.8-flash", "Qwen, швидка", 0.15, 0.47),
    ("qwen/qwen3.8-omni-flash", "Qwen omni", 0.15, 0.47),
    ("nvidia/nemotron-3.5-super-vl", "спеціалізована на зображеннях", 0.15, 0.5),
    ("mistralai/mistral-small-2603", "Mistral", 0.15, 0.6),
    ("meta/muse-ember", "Meta", 0.1, 0.5),
    ("tencent/hy4-flash", "найдешевша", 0.035, 0.14),
    ("google/gemini-3.7-flash", "Gemini, точніша", 0.75, 3.75),
    ("google/gemini-3.8-flash", "Gemini, найточніша з цих", 0.825, 4.125),
]


def estimate(price_in: float, price_out: float, tokens: tuple[int, int]) -> float:
    return (tokens[0] * price_in + tokens[1] * price_out) / 1e6


def catalog() -> dict:
    """For the Settings UI: both lists with a rough $ per request."""
    def rows(models, tokens):
        return [{"id": m, "note": n, "est": round(estimate(pi, po, tokens), 4)} for m, n, pi, po in models]
    return {"anchor": rows(ANCHOR_MODELS, ANCHOR_TOKENS), "follow": rows(FOLLOW_MODELS, FOLLOW_TOKENS)}
