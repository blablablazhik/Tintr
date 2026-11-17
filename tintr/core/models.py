"""Model choices offered in Settings (fal `openrouter/router/vision`; any other OpenRouter id still works).

Anchor costs are measured (one anchor each in the paid test). Follow prices: OpenRouter list prices,
$ per 1M tokens (input, output), checked 2026-11-17; a followed key ≈ 11k input + 1k output tokens
(claude-haiku-5.5: ~$0.0016). Follow models are NOT tested yet — compare them with tools/bench_vlm.py.
"""

from __future__ import annotations

FOLLOW_TOKENS = (11_000, 1_000)

# Anchor: the top 5 of the paid test on the Anim_2 anchor (42 regions, hand-checked answers, 2026-11-17),
# best first. (id, short note, measured $ per anchor). Dropped: gemini-3.8-flash (29/42, slow, cut-off
# answers), gpt-6.1-sol / -terra (17/42), qwen4-max (blocked by fal data policy), grok-4.7 (no answer).
ANCHOR_MODELS = [
    ("anthropic/claude-opus-5.5", "№1 у тесті: 37/42, 35 с", 0.064),
    ("google/gemini-4-carbon", "№2: 37/42, але 175 с", 0.271),
    ("anthropic/claude-sonnet-5.5", "№3: 34/42, найшвидша", 0.024),
    ("google/gemini-4-argon", "№4: 34/42", 0.034),
    ("google/gemini-4-hydrogen", "№5: 33/42", 0.066),
]

# Followed keys: (id, short note, input $/1M, output $/1M)
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
    anchor = [{"id": m, "note": n, "est": usd} for m, n, usd in ANCHOR_MODELS]
    follow = [{"id": m, "note": n, "est": round(estimate(pi, po, FOLLOW_TOKENS), 4)} for m, n, pi, po in FOLLOW_MODELS]
    return {"anchor": anchor, "follow": follow}
