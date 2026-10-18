"""Time about 20 short prompts against the brain and print p50/p95.

  python brain_latency.py
  python brain_latency.py --route
  python brain_latency.py --route --hub-timeout

Uses the womble-brain-xai Keychain item (account $USER), or XAI_BRAIN_API_KEY
if that is set. The key is never printed. Each prompt is a live Responses call
(grok-4.7, reasoning effort low, store false), so this spends a little credit.

Plain mode calls Brain.handle, the same function hub.route calls once the
regex misses. It does not load the hub, so it does not start Kokoro and it
does not see LaunchAgent-only environment variables. --route loads hub.py and
calls route() with the same first-sentence TTS hook the BOX-3 uses.

The speaking budget on the hub is BRAIN_TIMEOUT_S (default 8). This script
waits 30 s unless --hub-timeout is set, so the percentiles are the model's
and not that cutoff. Web search is offered only for live-fact wording; these
prompts do not ask for it. BRAIN_WEB_SEARCH=0 leaves it off entirely.
"""
import argparse, asyncio, os, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from brain.core import OFFLINE, Brain, _API_HOST, _families, _proxy_label
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


def _budget(hub_timeout: bool) -> float:
    if hub_timeout:
        return float(os.environ.get("BRAIN_TIMEOUT_S", "8"))
    return float(os.environ.get("BRAIN_LATENCY_TIMEOUT_S", "30"))


async def _via_handle(brain, prompt):
    reply = await brain.handle(prompt, source="box3-kitchen", session="box3-kitchen")
    return reply.llm_total_ms, reply.llm_ttft_ms, reply.category, reply.speech


async def _via_route(prompt):
    """hub.route plus the BOX-3 first-sentence TTS hook. Does not open the device."""
    os.environ.setdefault("BOX3_HOST", "127.0.0.1")
    os.environ.setdefault("BOX3_NOISE_PSK", "unused")
    os.environ.setdefault("HUB_HOST_IP", "127.0.0.1")
    os.environ.setdefault("TTS_ENGINE", os.environ.get("TTS_ENGINE", "kokoro"))
    import hub
    spoken = {"first": None, "rest": [], "text": []}
    res = await hub.route(
        prompt,
        source="box3-kitchen",
        session="box3-kitchen",
        on_sentence=lambda sentence: hub.begin_sentence_audio(spoken, sentence),
    )
    task = spoken["first"]
    if task is not None:
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"tts {type(exc).__name__}")
    return res.get("llm_total_ms"), res.get("llm_ttft_ms"), res.get("category"), res.get("speech")


async def main():
    parser = argparse.ArgumentParser(description="Time brain prompts and print p50/p95.")
    parser.add_argument("--route", action="store_true",
                        help="call hub.route with the BOX-3 first-sentence TTS hook")
    parser.add_argument("--hub-timeout", action="store_true",
                        help="use BRAIN_TIMEOUT_S (the speaking budget) instead of 30 s")
    args = parser.parse_args()
    if not get_key():
        print("No brain key. Add it with:")
        print("  security add-generic-password -a $USER -s womble-brain-xai -U -w")
        return 2
    key_source = "env" if os.environ.get("XAI_BRAIN_API_KEY", "").strip() else "keychain"
    try:
        dns = _families(_API_HOST)
    except Exception as exc:
        dns = type(exc).__name__
    print(f"proxy={_proxy_label()} dns={dns} key={key_source}")
    budget = _budget(args.hub_timeout)
    brain = None
    if args.route:
        os.environ.setdefault("BOX3_HOST", "127.0.0.1")
        os.environ.setdefault("BOX3_NOISE_PSK", "unused")
        os.environ.setdefault("HUB_HOST_IP", "127.0.0.1")
        import hub
        # route() uses the module globals. Point them at a throwaway list so a
        # latency run cannot write the hub's lists.json.
        store = ListStore(Path(tempfile.mkdtemp()) / "lists.json")
        hub.LISTS = store
        hub.BRAIN = Brain(store, hub.disp, timeout=budget)
    else:
        store = ListStore(Path(tempfile.mkdtemp()) / "lists.json")
        brain = Brain(store, timeout=budget)
    totals, ttfts, failed = [], [], 0
    mode = "route" if args.route else "handle"
    print(f"{'ms':>8}  {'ttft':>8}  prompt   ({mode}, budget {budget:.0f}s)")
    for prompt in PROMPTS:
        if args.route:
            total, ttft, category, speech = await _via_route(prompt)
        else:
            total, ttft, category, speech = await _via_handle(brain, prompt)
        totals.append(total or 0)
        if ttft is not None:
            ttfts.append(ttft)
        if category != "brain" or speech == OFFLINE:
            failed += 1
        print(f"{fmt(total):>8}  {fmt(ttft):>8}  {prompt}")
    print()
    print(f"runs {len(PROMPTS)}  failed {failed}  mode={mode}  "
          f"web_search={os.environ.get('BRAIN_WEB_SEARCH', '1')}")
    for label, values in (("total", totals), ("ttft", ttfts)):
        print(f"{label:5}  p50 {fmt(percentile(values, 50))} ms   "
              f"p90 {fmt(percentile(values, 90))} ms   "
              f"p95 {fmt(percentile(values, 95))} ms")
    return 1 if failed == len(PROMPTS) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
