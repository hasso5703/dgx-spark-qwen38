#!/usr/bin/env python3
"""The engine's own counters (engine_metrics.py): what is read, and what the window says.

The window answers four questions about the last 5 minutes (how much of the prompts the
cache held, how fast each request decoded, how long a request waited for its first token,
how many tokens all clients got per second) from two reads of SGLang's counters. These
tests hold the reading (every label set summed, nothing but the eight counters), the
arithmetic, and the two things that are not the clients': the cockpit's canary, taken out
exactly, and an engine that started again, whose counters started again from zero.
"""
import importlib.util
import pathlib
import random
import unittest

DASH = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("engine_metrics_under_test", DASH / "engine_metrics.py")
em = importlib.util.module_from_spec(spec)
spec.loader.exec_module(em)

L = 'engine_type="unified",model_name="qwen3.8-27b",pp_rank="0",tp_rank="0"'


def page(requests=0, prompt=0, cached_device=0, cached_host=0, generated=0,
         ttft_sum=0.0, ttft_count=0, itl_sum=0.0, itl_count=0, streaming_split=True):
    """A /metrics page in the shape SGLang's multiprocess collector writes."""
    half = (lambda v: (v // 2, v - v // 2)) if streaming_split else (lambda v: (v, 0))
    pr, ps = half(prompt)
    gr, gs = half(generated)
    rr, rs = half(requests)
    return "\n".join([
        "# HELP sglang:prompt_tokens_total Number of prefill tokens processed.",
        "# TYPE sglang:prompt_tokens_total counter",
        f'sglang:prompt_tokens_total{{{L},is_streaming="false"}} {pr}.0',
        f'sglang:prompt_tokens_total{{{L},is_streaming="true"}} {ps}.0',
        f'sglang:prompt_tokens_created{{{L},is_streaming="true"}} 1.7917e+09',
        f'sglang:generation_tokens_total{{{L},is_streaming="false"}} {gr}.0',
        f'sglang:generation_tokens_total{{{L},is_streaming="true"}} {gs}.0',
        f'sglang:num_requests_total{{{L},is_streaming="false"}} {rr}.0',
        f'sglang:num_requests_total{{{L},is_streaming="true"}} {rs}.0',
        f'sglang:cached_tokens_total{{{L},cache_source="device"}} {cached_device}.0',
        f'sglang:cached_tokens_total{{{L},cache_source="host"}} {cached_host}.0',
        "# TYPE sglang:time_to_first_token_seconds histogram",
        f'sglang:time_to_first_token_seconds_bucket{{{L},is_streaming="true",le="0.1"}} {ttft_count}.0',
        f'sglang:time_to_first_token_seconds_bucket{{{L},is_streaming="true",le="+Inf"}} {ttft_count}.0',
        f'sglang:time_to_first_token_seconds_count{{{L},is_streaming="true"}} {ttft_count}.0',
        f'sglang:time_to_first_token_seconds_sum{{{L},is_streaming="true"}} {ttft_sum}',
        f'sglang:inter_token_latency_seconds_bucket{{{L},le="0.05"}} {itl_count}.0',
        f'sglang:inter_token_latency_seconds_count{{{L}}} {itl_count}.0',
        f'sglang:inter_token_latency_seconds_sum{{{L}}} {itl_sum}',
        f'sglang:inter_token_latency_seconds_created{{{L}}} 1.7917e+09',
        'sglang:cache_hit_rate{%s} 0.93' % L,
        "", ])


class Reading(unittest.TestCase):
    def test_every_label_set_is_summed(self):
        v = em.parse(page(requests=7, prompt=1001, cached_device=600, cached_host=100, generated=33,
                          ttft_sum=2.5, ttft_count=7, itl_sum=1.25, itl_count=26))
        self.assertEqual(v, {"requests": 7.0, "prompt": 1001.0, "cached": 700.0, "generated": 33.0,
                             "ttft_sum": 2.5, "ttft_count": 7.0, "itl_sum": 1.25, "itl_count": 26.0})

    def test_created_buckets_and_gauges_are_not_counters(self):
        v = em.parse(page(ttft_count=3))
        self.assertEqual(v["ttft_count"], 3.0, "the le= buckets are not added to the count")

    def test_a_page_that_is_not_an_engines_is_none(self):
        for text in ("", "hello", "# HELP x\n# TYPE x counter\nx_total 3\n", "<html>404</html>"):
            self.assertIsNone(em.parse(text), text)

    def test_odd_values_are_skipped(self):
        v = em.parse(f'sglang:num_requests_total{{{L}}} NaN\nsglang:num_requests_total{{x="1"}} 4\n'
                     'sglang:prompt_tokens_total +Inf\nsglang:prompt_tokens_total{a="b"} junk\n')
        self.assertEqual((v["requests"], v["prompt"]), (4.0, 0.0))

    def test_the_real_page_of_a_27b_just_ready(self):
        # captured on the reference box (2026-10-07), right after the lane was ready: its own
        # warm-up request is the one counted, and the families it never fed are not listed
        page_ = (DASH / "tests" / "fixtures" / "sglang-metrics-27b-ready.txt").read_text()
        self.assertNotIn("cached_tokens_total{", page_, "the fixture is the page as it was")
        v = em.parse(page_)
        self.assertEqual(v, {"requests": 1.0, "prompt": 178.0, "cached": 0.0, "generated": 8.0,
                             "ttft_sum": 12.01112219899369, "ttft_count": 1.0,
                             "itl_sum": 0.0, "itl_count": 0.0})

    def test_an_engine_page_with_no_request_yet_is_zeros_not_none(self):
        v = em.parse("# HELP sglang:num_requests_total The number of requests.\n"
                     "# TYPE sglang:num_requests_total counter\n"
                     'sglang:num_running_reqs{model_name="m"} 0.0\n')
        self.assertEqual(v, dict.fromkeys(em.NAMES, 0.0))

    def test_a_trailing_timestamp_is_not_the_value(self):
        v = em.parse(f'sglang:num_requests_total{{{L}}} 12 1791322000000\n')
        self.assertEqual(v["requests"], 12.0)


def vals(**kw):
    out = dict.fromkeys(em.NAMES, 0.0)
    out.update({k: float(v) for k, v in kw.items()})
    return out


class TheWindow(unittest.TestCase):
    KEY = ("qwen38-sglang.service", "123")

    def test_two_reads_say_the_four_numbers(self):
        w = em.Window(300)
        w.add(1000, self.KEY, vals(requests=10, prompt=50_000, cached=40_000, generated=2_000,
                                   ttft_sum=5, ttft_count=10, itl_sum=40, itl_count=1_990))
        w.add(1300, self.KEY, vals(requests=14, prompt=250_000, cached=230_000, generated=3_000,
                                   ttft_sum=9, ttft_count=14, itl_sum=60, itl_count=2_986))
        s = w.stats(1300)
        self.assertEqual((s["requests"], s["prompt_tokens"], s["cached_tokens"], s["generated_tokens"]),
                         (4, 200_000, 190_000, 1_000))
        self.assertAlmostEqual(s["reuse"], 0.95)
        self.assertAlmostEqual(s["decode_tps"], 996 / 20)
        self.assertAlmostEqual(s["ttft_s"], 1.0)
        self.assertAlmostEqual(s["throughput_tps"], 1_000 / 300)
        self.assertEqual((s["watched_s"], s["canaries_out"], s["approximate"]), (300.0, 0, False))

    def test_a_younger_window_says_how_long_it_watched(self):
        w = em.Window(300)
        w.add(1000, self.KEY, vals())
        w.add(1090, self.KEY, vals(requests=1, prompt=100, generated=10, ttft_sum=0.2, ttft_count=1,
                                   itl_sum=0.3, itl_count=9))
        self.assertEqual(w.stats(1090)["watched_s"], 90.0)

    def test_the_window_starts_at_the_last_read_before_its_span(self):
        w = em.Window(300)
        for t, n in ((0, 0), (100, 1), (200, 2), (305, 3), (410, 4), (520, 5)):
            w.add(t, self.KEY, vals(requests=n))
        self.assertEqual(w.samples[0][0], 200, "520 - 300 = 220: the read at 200 starts it")
        self.assertEqual(w.stats(520)["requests"], 3)

    def test_nothing_happened_is_no_number(self):
        w = em.Window(300)
        w.add(0, self.KEY, vals(requests=5, prompt=10))
        w.add(300, self.KEY, vals(requests=5, prompt=10))
        s = w.stats(300)
        self.assertEqual(s["requests"], 0)
        self.assertEqual((s["reuse"], s["decode_tps"], s["ttft_s"]), (None, None, None))
        self.assertEqual(s["throughput_tps"], 0.0)

    def test_a_request_still_decoding_has_a_speed_before_it_finishes(self):
        # the inter-token counters move with every output; the request counters at its end
        w = em.Window(300)
        w.add(0, self.KEY, vals())
        w.add(30, self.KEY, vals(ttft_sum=0.4, ttft_count=1, itl_sum=20, itl_count=900))
        s = w.stats(30)
        self.assertEqual(s["requests"], 0)
        self.assertAlmostEqual(s["decode_tps"], 45.0)
        self.assertAlmostEqual(s["ttft_s"], 0.4)

    def test_a_new_engine_starts_the_window_again(self):
        w = em.Window(300)
        w.add(0, self.KEY, vals(requests=100))
        w.add(100, ("qwen38-flash.service", "999"), vals(requests=3))
        w.add(150, ("qwen38-flash.service", "999"), vals(requests=4))
        s = w.stats(150)
        self.assertEqual((s["requests"], s["watched_s"]), (1, 50.0))

    def test_counters_that_went_back_start_it_again(self):
        # the same unit restarted between two reads that both saw it serving
        w = em.Window(300)
        w.add(0, self.KEY, vals(requests=100, prompt=9_000))
        w.add(10, self.KEY, vals(requests=2, prompt=300))
        w.add(20, self.KEY, vals(requests=3, prompt=400))
        s = w.stats(20)
        self.assertEqual((s["requests"], s["prompt_tokens"], s["watched_s"]), (1, 100, 10.0))


CANARY = vals(requests=1, prompt=14, generated=2, ttft_sum=0.08, ttft_count=1, itl_sum=0.02, itl_count=1)


def plus(a, b):
    return {k: a[k] + b[k] for k in em.NAMES}


class TheCanaryIsTakenOut(unittest.TestCase):
    KEY = ("qwen38-flash.service", "7")

    def test_an_idle_lane_with_canaries_says_nothing_happened(self):
        w = em.Window(300)
        v = vals()
        w.add(0, self.KEY, v)
        for t in (90, 180, 270):
            before = v
            v = plus(v, CANARY)
            w.canary(t, self.KEY, before, v)
            w.add(t + 1, self.KEY, v)
        s = w.stats(271)
        self.assertEqual((s["requests"], s["prompt_tokens"], s["generated_tokens"]), (0, 0, 0))
        self.assertEqual((s["reuse"], s["decode_tps"], s["ttft_s"]), (None, None, None))
        self.assertEqual((s["canaries_out"], s["approximate"]), (3, False))

    def test_client_numbers_are_exact_with_a_canary_among_them(self):
        w = em.Window(300)
        client = vals(requests=2, prompt=40_000, cached=39_000, generated=800,
                      ttft_sum=1.0, ttft_count=2, itl_sum=19.0, itl_count=798)
        w.add(0, self.KEY, vals())
        w.canary(100, self.KEY, vals(), CANARY)
        w.add(200, self.KEY, plus(CANARY, client))
        s = w.stats(200)
        self.assertEqual((s["requests"], s["prompt_tokens"], s["cached_tokens"], s["generated_tokens"]),
                         (2, 40_000, 39_000, 800))
        self.assertAlmostEqual(s["decode_tps"], 798 / 19.0)
        self.assertAlmostEqual(s["ttft_s"], 0.5)

    def test_a_canary_that_overlapped_a_client_makes_the_window_approximate(self):
        w = em.Window(300)
        w.add(0, self.KEY, vals())
        overlapped = plus(CANARY, vals(requests=1, prompt=500, ttft_sum=0.3, ttft_count=1))
        w.canary(50, self.KEY, vals(), overlapped)
        w.add(60, self.KEY, overlapped)
        self.assertTrue(w.stats(60)["approximate"])
        self.assertEqual(w.stats(60)["canaries_out"], 0, "nothing is taken out that is not the canary's alone")
        w.add(400, self.KEY, overlapped)
        self.assertFalse(w.stats(400)["approximate"], "once that canary has left the window")

    def test_a_canary_with_a_missed_read_makes_it_approximate(self):
        w = em.Window(300)
        w.add(0, self.KEY, vals())
        w.canary(10, self.KEY, None, CANARY)
        self.assertFalse(w.stats(10)["approximate"], "no read holds it yet")
        w.add(20, self.KEY, CANARY)
        s = w.stats(20)
        self.assertTrue(s["approximate"])
        self.assertEqual((s["requests"], s["canaries_out"]), (1, 0), "counted, and said to be")
        w.add(330, self.KEY, CANARY)
        self.assertFalse(w.stats(330)["approximate"], "once the window starts after it")

    def test_a_canary_of_another_engine_changes_nothing(self):
        w = em.Window(300)
        w.add(0, self.KEY, vals())
        w.canary(10, ("qwen38-sglang.service", "1"), vals(), CANARY)
        self.assertEqual(list(w.canaries), [])
        self.assertFalse(w.stats(20)["approximate"])

    def test_a_canary_before_the_window_is_not_taken_out_of_it(self):
        w = em.Window(300)
        w.add(0, self.KEY, vals())
        w.canary(10, self.KEY, vals(), CANARY)
        w.add(20, self.KEY, CANARY)
        w.add(330, self.KEY, plus(CANARY, vals(requests=1, prompt=10)))
        s = w.stats(330)
        self.assertEqual((s["requests"], s["prompt_tokens"], s["canaries_out"]), (1, 10, 0))


class AgainstAReplay(unittest.TestCase):
    """Random traffic and canaries with the timing of the box, each step at its own instant:
    a read of the window takes its values when it starts and is added when it ends; the
    canary reads, its request is counted, it reads again. After EVERY read the window's
    numbers equal the sums over the client requests between the instants its two ends took
    their values, unless the window says itself it is approximate (a client finished inside
    a canary). No read across a canary is kept, so no canary is taken out of a window that
    does not hold it, or left in one that does."""

    def test_random_traffic_at_every_read(self):
        rnd = random.Random(20261007)
        checked = dropped = approximate = 0
        for trial in range(60):
            w = em.Window(300)
            key = ("qwen38-flash.service", str(trial))
            events = []             # (instant, order, kind, payload)
            t = 0.0
            while t < 900:
                for _ in range(rnd.randint(0, 2)):
                    p = rnd.randint(1, 60_000)
                    events.append((t + rnd.uniform(0, 5), 0, "client", vals(
                        requests=1, prompt=p, cached=rnd.randint(0, p), generated=rnd.randint(1, 900),
                        ttft_sum=rnd.uniform(0.05, 30), ttft_count=1,
                        itl_sum=rnd.uniform(0.1, 40), itl_count=rnd.randint(0, 899))))
                if rnd.random() < 0.15:
                    b, x = t + rnd.uniform(0, 4.5), rnd.uniform(0.05, 0.5)
                    events += [(b, 1, "canary_begins", None), (b + x / 2, 2, "canary_counted", None),
                               (b + x, 3, "canary_ends", None)]
                r, d = t + 5 - rnd.uniform(0, 0.2), rnd.uniform(0.001, 0.3)
                events += [(r, 4, "read_starts", r), (r + d, 5, "read_ends", r)]
                t += 5
            events.sort(key=lambda e: (e[0], e[1]))
            total, finished, snaps, took, before = vals(), [], {}, {}, None
            for at, _, kind, x in events:
                if kind == "client":
                    total = plus(total, x)
                    finished.append((at, x))
                elif kind == "canary_begins":
                    w.canary_begins(at)
                    before = total
                elif kind == "canary_counted":
                    total = plus(total, CANARY)
                elif kind == "canary_ends":
                    w.canary(at, key, before, total)
                elif kind == "read_starts":
                    snaps[x] = total
                else:
                    kept = w.add(at, key, snaps.pop(x), started=x)
                    dropped += not kept
                    if kept:
                        took[at] = x
                    if len(w.samples) < 2:
                        continue
                    s = w.stats(at)
                    if s["approximate"]:
                        approximate += 1
                        continue
                    t0, t1 = took[w.samples[0][0]], took[w.samples[-1][0]]
                    mine = [c for tc, c in finished if t0 < tc <= t1]
                    want = {k: sum(c[k] for c in mine) for k in em.NAMES}
                    self.assertEqual(s["requests"], len(mine), (trial, at))
                    self.assertEqual(s["prompt_tokens"], want["prompt"])
                    self.assertEqual(s["cached_tokens"], want["cached"])
                    self.assertEqual(s["generated_tokens"], want["generated"])
                    if want["ttft_count"]:
                        self.assertAlmostEqual(s["ttft_s"], want["ttft_sum"] / want["ttft_count"], places=6)
                    if want["itl_count"]:
                        self.assertAlmostEqual(s["decode_tps"], want["itl_count"] / want["itl_sum"], places=6)
                    checked += 1
        self.assertGreater(checked, 5000)
        self.assertGreater(dropped, 0, "the replay never put a read across a canary")
        self.assertGreater(approximate, 0, "the replay never put a client inside a canary")


class AReadAcrossTheCanaryIsDropped(unittest.TestCase):
    KEY = ("qwen38-flash.service", "7")

    def test_a_read_while_the_canary_is_in_flight(self):
        w = em.Window(300)
        w.add(0, self.KEY, vals())
        w.canary_begins(10)
        self.assertFalse(w.add(11, self.KEY, CANARY, started=10.5))
        w.canary(12, self.KEY, vals(), CANARY)
        self.assertTrue(w.add(15, self.KEY, CANARY, started=14.9))
        self.assertEqual((w.stats(15)["requests"], w.stats(15)["canaries_out"]), (0, 1))

    def test_a_read_that_began_before_the_canary_ended_after_it(self):
        w = em.Window(300)
        w.add(0, self.KEY, vals())
        w.canary_begins(10)
        w.canary(12, self.KEY, vals(), CANARY)
        self.assertFalse(w.add(13, self.KEY, vals(), started=9.9), "it may hold the canary or not")
        self.assertEqual(w.stats(13)["requests"], 0)

    def test_a_canary_that_never_said_it_ended_stops_blinding_after_a_minute(self):
        w = em.Window(300)
        w.add(0, self.KEY, vals())
        w.canary_begins(10)
        self.assertFalse(w.add(65, self.KEY, vals(), started=64.9))
        self.assertTrue(w.add(75, self.KEY, vals(), started=74.9))


if __name__ == "__main__":
    unittest.main()
