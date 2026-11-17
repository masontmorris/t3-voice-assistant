import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

from voice.local_decider.client import D1Client, from_systemone, to_systemone


class Adapter(unittest.TestCase):
    def test_the_request_keeps_state_and_questions_and_drops_the_openrouter_model(self):
        body = {"model": "perplexity/pplx-decider-flash-v1.3:free", "state": {"transcript": "hi"},
                "questions": {"s1_kind": {"type": "choice", "instructions": "x", "criteria": {"a": "A", "b": "B"}}}}
        self.assertEqual(to_systemone(body), {"state": body["state"], "questions": body["questions"]})

    def test_choice_confidence_is_rescaled_to_zero_at_chance(self):
        response = {"answers": {
            "two": {"type": "choice", "choice": "a", "probabilities": {"a": 0.75, "b": 0.25}, "confidence": 0.75},
            "three": {"type": "choice", "choice": "a", "probabilities": {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3},
                      "confidence": 1 / 3},
            "yes": {"type": "noul", "noul": 0.2}}}
        answers = from_systemone(response)["answers"]
        self.assertAlmostEqual(answers["two"]["confidence"], 0.5)
        self.assertAlmostEqual(answers["three"]["confidence"], 0.0)
        self.assertEqual(answers["two"]["top_probability"], 0.75)
        self.assertEqual(answers["yes"], {"type": "noul", "noul": 0.2})

    def test_a_response_without_answers_passes_through(self):
        self.assertEqual(from_systemone({"error": "bad"}), {"error": "bad"})


class Handler(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.seen.append((self.path, body))
        answers = {name: {"type": "choice", "choice": next(iter(q["criteria"])),
                          "probabilities": {k: 1 / len(q["criteria"]) for k in q["criteria"]},
                          "confidence": 1 / len(q["criteria"])} for name, q in body["questions"].items()}
        raw = json.dumps({"model": "d1", "answers": answers, "usage": {"input_tokens": 7, "output_tokens": 0}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class Client(unittest.TestCase):
    def test_a_call_posts_to_systemone_and_returns_a_cloud_shaped_record(self):
        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        body = {"model": "m:free", "state": {"segments": {}},
                "questions": {"s1_ready": {"type": "choice", "instructions": "x", "criteria": {"wait": "W", "go": "G"}}}}
        client = D1Client(f"http://127.0.0.1:{server.server_port}")
        self.addCleanup(client.close)
        record = client.call("decisions", body, label="u:r1")
        path, sent = Handler.seen[-1]
        self.assertEqual(path, "/v1/systemone")
        self.assertNotIn("model", sent)
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["request"], body)
        self.assertEqual(record["response"]["answers"]["s1_ready"]["confidence"], 0.0)
        self.assertGreater(record["timing"]["ms"], 0)

    def test_a_set_size_rides_with_the_request_and_is_absent_when_unset(self):
        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        body = {"state": {"segments": {}},
                "questions": {"s1_ready": {"type": "choice", "instructions": "x", "criteria": {"wait": "W", "go": "G"}}}}
        packed = D1Client(f"http://127.0.0.1:{server.server_port}", set_size=4)
        self.addCleanup(packed.close)
        packed.call("decisions", body)
        self.assertEqual(Handler.seen[-1][1]["set_size"], 4)
        unpacked = D1Client(f"http://127.0.0.1:{server.server_port}")
        self.addCleanup(unpacked.close)
        unpacked.call("decisions", body)
        self.assertNotIn("set_size", Handler.seen[-1][1])


class Held(Handler):
    """Answers only once `release` is set, as a server still working on a long request does."""

    release, arrived = threading.Event(), threading.Event()

    def do_POST(self):
        Held.arrived.set()
        Held.release.wait(10)
        try:
            super().do_POST()
        except ConnectionError:
            pass


class Lane(unittest.TestCase):
    BODY = {"state": {"segments": {}},
            "questions": {"s1_ready": {"type": "choice", "instructions": "x", "criteria": {"wait": "W", "go": "G"}}}}

    def setUp(self):
        Held.release, Held.arrived, Handler.seen = threading.Event(), threading.Event(), []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Held)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(Held.release.set)
        self.turns = D1Client(f"http://127.0.0.1:{self.server.server_port}")
        self.addCleanup(self.turns.close)

    def test_abort_ends_a_request_the_server_is_still_working_on(self):
        lane = self.turns.lane(5.0)
        self.addCleanup(lane.close)
        failed = []
        worker = threading.Thread(target=lambda: self._call(lane, failed))
        worker.start()
        self.assertTrue(Held.arrived.wait(5))
        lane.abort()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "the call returned without the server's answer")
        self.assertFalse(Held.release.is_set())
        self.assertEqual(len(failed), 1)
        Held.release.set()
        self.assertEqual(self.turns.call("decisions", self.BODY)["status"], 200, "the turns' own client is untouched")

    def test_a_lane_aborted_before_it_asks_sends_nothing(self):
        lane = self.turns.lane(5.0)
        lane.abort()
        with self.assertRaises(ConnectionError):
            lane.call("decisions", self.BODY)
        self.assertEqual(Handler.seen, [])

    @staticmethod
    def _call(lane, failed):
        try:
            lane.call("decisions", Lane.BODY)
        except (ConnectionError, OSError) as exc:
            failed.append(exc)


if __name__ == "__main__":
    unittest.main()
