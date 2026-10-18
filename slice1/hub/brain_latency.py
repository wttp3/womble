"""Time about 20 short prompts against the brain and print p50/p95.

  python brain_latency.py

Uses the womble-brain-xai Keychain item (account $USER), or XAI_BRAIN_API_KEY
if that is set. The key is never printed. Each prompt is a live Responses call
(grok-4.7, reasoning effort low, store false), so this spends a little credit.
The hub's speaking budget stays BRAIN_TIMEOUT_S (4 s). This script waits up to
30 s so the percentiles are the model's, not that cutoff.

Web search follows BRAIN_WEB_SEARCH (default on). These prompts are answerable
without it.
"""
import asyncio, os, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from brain.core import OFFLINE, Brain
from brain.key import get_key
from lists import ListStore

PROMPTS = [
    "What is two plus two?",
    "Say hello in one sentence.",
    "What is a simple breakfast?",
    "How do you boil an egg?",
    "What day comes after Tuesday?",
    "Give me a short kitchen tip.",
    "What is the capital of Spain?",
    "How many grams are in a kilogram?",
    "What is a scone?",
    "Suggest a quick pasta.",
    "What does simmer mean?",
    "How long should tea brew?",
    "What can stand in for buttermilk?",
    "Name a calm colour.",
    "What is the opposite of salty?",
    "How can you tell an avocado is ripe?",
    "What is a dozen?",
    "Tell me a one-sentence kitchen joke.",
    "What is the boiling point of water in celsius?",
    "Say good night in one sentence.",
]


def percentile(values, p):
    xs = sorted(values)
    if not xs:
        return None
    if len(xs) == 1:
        return float(xs[0])
    k = (len(xs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def fmt(ms):
    return "-" if ms is None else str(round(ms))


async def main():
    if not get_key():
        print("No brain key. Add it with:")
        print("  security add-generic-password -a $USER -s womble-brain-xai -U -w")
        return 2
    store = ListStore(Path(tempfile.mkdtemp()) / "lists.json")
    brain = Brain(store, timeout=float(os.environ.get("BRAIN_LATENCY_TIMEOUT_S", "30")))
    totals, ttfts, failed = [], [], 0
    print(f"{'ms':>8}  {'ttft':>8}  prompt")
    for prompt in PROMPTS:
        reply = await brain.handle(prompt, source="latency", session="latency")
        totals.append(reply.llm_total_ms or 0)
        if reply.llm_ttft_ms is not None:
            ttfts.append(reply.llm_ttft_ms)
        if reply.category != "brain" or reply.speech == OFFLINE:
            failed += 1
        print(f"{fmt(reply.llm_total_ms):>8}  {fmt(reply.llm_ttft_ms):>8}  {prompt}")
    print()
    print(f"runs {len(PROMPTS)}  failed {failed}  web_search={os.environ.get('BRAIN_WEB_SEARCH', '1')}")
    for label, values in (("total", totals), ("ttft", ttfts)):
        print(f"{label:5}  p50 {fmt(percentile(values, 50))} ms   "
              f"p90 {fmt(percentile(values, 90))} ms   "
              f"p95 {fmt(percentile(values, 95))} ms")
    return 1 if failed == len(PROMPTS) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
