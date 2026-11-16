"""The hosted d1.2 client: what goes on the wire, what the ledger records, and where it stops.

These stand for Liquid's endpoint with a local HTTP server, so no test spends money. Each test gives the server its
own scripted responses: a status, headers and a body.
"""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from voice.liquid_decider import client as liquid

KEY = "test-key-never-logged"


def answer(questions, cost=0.00001):
    return {"model": "d1.2",
            "answers": {name: {"type": "choice", "choice": next(iter(question["criteria"])),
                               "probabilities": {option: 1 / len(question["criteria"])
                                                 for option in question["criteria"]},
                               "confidence": 0.0} for name, question in questions.items()},
            "usage": {"input_tokens": 10, "output_tokens": 0, "cost": cost}}


def body(questions=1):
    return {"model": "perplexity/pplx-decider-flash-v1.3:free", "state": {"transcript": "hi"},
            "questions": {f"s{i}_ready": {"type": "choice", "instructions": "x",
                                          "criteria": {"wait": "W", "go": "G"}}
                          for i in range(questions)}}


class Handler(BaseHTTPRequestHandler):
    scripted, seen = [], []

    def do_POST(self):
        sent = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.seen.append((self.path, sent, self.headers.get("Authorization")))
        status, headers, payload = Handler.scripted.pop(0) if Handler.scripted else (200, {}, None)
        raw = json.dumps(payload if payload is not None else answer(sent["questions"])).encode()
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class LiquidCase(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        Handler.scripted, Handler.seen = [], []
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        liquid.use_cache(Path(self.dir.name))
        os.environ["LIQUID_API_KEY"] = KEY
        self.addCleanup(os.environ.pop, "LIQUID_API_KEY", None)
        self.client = liquid.LiquidClient(f"http://127.0.0.1:{self.server.server_port}")
        self.addCleanup(self.client.close)

    def ledger(self):
        return liquid.entries()


class Request(LiquidCase):
    def test_the_wire_body_names_d1_2_and_keeps_state_and_questions(self):
        record = self.client.call("decisions", body(), label="u:r1")
        path, sent, authorization = Handler.seen[-1]
        self.assertEqual(path, liquid.PATH)
        self.assertEqual(sent["model"], "d1.2")
        self.assertEqual(sent["state"], {"transcript": "hi"})
        self.assertEqual(list(sent["questions"]), ["s0_ready"])
        self.assertEqual(authorization, f"Bearer {KEY}")
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["request"]["model"], "perplexity/pplx-decider-flash-v1.3:free")
        self.assertGreater(record["timing"]["ms"], 0)
        self.assertEqual(record["requests"], 1)

    def test_answers_come_back_on_the_scale_the_gate_reads(self):
        probabilities = {"go": 0.7, "wait": 0.3}
        liquid_body = {"model": "d1.2",
                       "answers": {"s0_ready": {"type": "choice", "choice": "go",
                                                "probabilities": probabilities, "confidence": 0.4}},
                       "usage": {"input_tokens": 5, "output_tokens": 0, "cost": 0.00002}}
        Handler.scripted = [(200, {}, liquid_body)]
        record = self.client.call("decisions", body(), label="u:r1")
        self.assertEqual(record["response"]["answers"], liquid_body["answers"])
        self.assertAlmostEqual(record["response"]["answers"]["s0_ready"]["confidence"], 0.4)

    def test_a_body_over_the_question_limit_is_sent_in_two_requests_and_merged(self):
        record = self.client.call("decisions", body(liquid.MAX_QUESTIONS + 1), label="big")
        self.assertEqual(len(Handler.seen), 2)
        self.assertEqual(len(record["response"]["answers"]), liquid.MAX_QUESTIONS + 1)
        self.assertEqual(record["response"]["usage"]["input_tokens"], 20)
        self.assertAlmostEqual(record["response"]["usage"]["cost"], 0.00002)
        self.assertEqual([entry["label"] for entry in self.ledger()], ["big#1", "big#2"])

    def test_the_key_never_reaches_the_ledger(self):
        self.client.call("decisions", body(), label="u:r1")
        text = liquid.LEDGER.read_text(encoding="utf-8")
        self.assertNotIn(KEY, text)
        self.assertNotIn("Bearer", text)


class Ledger(LiquidCase):
    def test_the_ledger_records_each_call_s_reported_cost(self):
        self.client.call("decisions", body(), label="u:r1")
        self.client.call("decisions", body(2), label="u:r2")
        entries = self.ledger()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["input_tokens"], 10)
        self.assertEqual(entries[0]["output_tokens"], 0)
        self.assertAlmostEqual(liquid.spend_today(), 0.00002)

    def test_a_cached_body_makes_no_request_and_no_new_charge(self):
        self.client.call("decisions", body(), label="u:r1")
        self.client.call("decisions", body(), label="u:r1")
        self.assertEqual(len(Handler.seen), 1)
        self.assertEqual(len(self.ledger()), 1)

    def test_a_fresh_call_sends_again_without_overwriting_the_cache(self):
        first = self.client.call("decisions", body(), label="u:r1")
        second = self.client.call("decisions", body(), label="u:r1", fresh=True)
        self.assertEqual(len(Handler.seen), 2)
        self.assertEqual(len(self.ledger()), 2)
        self.assertIsNot(first, second)
        self.assertEqual(liquid.cached("decisions", body())["timing"], first["timing"])


class Spending(LiquidCase):
    def test_a_call_that_would_start_past_the_day_limit_is_refused(self):
        self.client.call("decisions", body(), label="u:r1")
        liquid.LEDGER.write_text(liquid.LEDGER.read_text(encoding="utf-8").replace(
            '"cost": 1e-05', '"cost": 1.0'), encoding="utf-8")
        with self.assertRaises(liquid.SpendStop):
            self.client.call("decisions", body(2), label="u:r2")
        self.assertEqual(len(Handler.seen), 1)
        self.assertTrue(liquid.STOP.exists())
        with self.assertRaises(liquid.SpendStop):
            self.client.call("decisions", body(3), label="u:r3")

    def test_a_day_below_the_limit_lets_calls_through(self):
        liquid.LEDGER.parent.mkdir(parents=True, exist_ok=True)
        self.assertAlmostEqual(liquid.spend_today(), 0.0)
        self.client.call("decisions", body(), label="u:r1")
        self.assertFalse(liquid.STOP.exists())


class Limits(LiquidCase):
    def test_a_429_waits_on_retry_after_and_is_not_a_billing_stop(self):
        Handler.scripted = [(429, {"Retry-After": "0"}, {"type": "rate_limit_error", "error": "slow down"})]
        record = self.client.call("decisions", body(), label="u:r1")
        self.assertEqual(len(Handler.seen), 2)
        self.assertEqual(record["status"], 200)
        self.assertEqual([entry["status"] for entry in self.ledger()], [429, 200])
        self.assertFalse(liquid.BILLING.exists())
        self.assertFalse(liquid.STOP.exists())

    def test_a_402_stops_calls_for_billing(self):
        Handler.scripted = [(402, {}, {"error": {"message": "insufficient credit"}})]
        with self.assertRaises(liquid.BillingStop):
            self.client.call("decisions", body(), label="u:r1")
        self.assertTrue(liquid.BILLING.exists())
        with self.assertRaises(liquid.BillingStop):
            self.client.call("decisions", body(2), label="u:r2")

    def test_a_500_is_retried_and_the_record_keeps_every_attempt(self):
        Handler.scripted = [(500, {}, {"error": "boom"})]
        record = self.client.call("decisions", body(), label="u:r1")
        self.assertEqual(record["status"], 200)
        self.assertEqual([entry["status"] for entry in record["attempts"]], [500, 200])


if __name__ == "__main__":
    unittest.main()
