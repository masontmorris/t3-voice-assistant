import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

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


if __name__ == "__main__":
    unittest.main()
