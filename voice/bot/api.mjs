// The bot's localhost API. serve.mjs mounts it on 127.0.0.1. See serve.mjs for the routes.
import { ack, handoff, snapshot } from "./root.mjs";

// deps: { data, save, t3, waiters: Set<() => void>, say }
export function createApiHandler({ data, save, t3, waiters, say }) {
	const json = (res, code, body) => {
		res.writeHead(code, { "content-type": "application/json" });
		res.end(JSON.stringify(body));
	};
	const body = (req) =>
		new Promise((resolve) => {
			let s = "";
			req.on("data", (c) => (s += c));
			req.on("end", () => resolve(s ? JSON.parse(s) : {}));
		});
	const after = (id) => data.outbox.slice(id ? data.outbox.findIndex((e) => e.id === id) + 1 : 0);

	return async (req, res) => {
		try {
			const url = new URL(req.url, "http://127.0.0.1");
			if (req.method === "POST" && url.pathname === "/handoff") {
				const r = await body(req);
				if (!r.requestId || !r.text) return json(res, 400, { error: "requestId and text are required" });
				const out = await handoff(data, t3, r, save);
				say(`handoff ${r.requestId}: ${out.status}`);
				return json(res, 200, out);
			}
			if (req.method === "GET" && url.pathname === "/outbox") {
				const since = url.searchParams.get("after");
				const waitMs = Math.min(Number(url.searchParams.get("waitMs") || 0), 60000);
				if (!after(since).length && waitMs > 0)
					await new Promise((resolve) => {
						const done = () => {
							waiters.delete(done);
							resolve();
						};
						waiters.add(done);
						setTimeout(done, waitMs);
					});
				return json(res, 200, { entries: after(since) });
			}
			if (req.method === "POST" && url.pathname === "/outbox/ack") {
				ack(data, (await body(req)).ids || []);
				save();
				return json(res, 200, { ok: true });
			}
			if (req.method === "GET" && url.pathname === "/snapshot") {
				const q = url.searchParams;
				return json(res, 200, snapshot(data, { conversationId: q.get("conversation"), utteranceId: q.get("utterance"), finalTranscript: q.get("transcript") || "", requestPrefix: q.get("prefix") || "" }));
			}
			if (req.method === "GET" && url.pathname === "/registry") return json(res, 200, { root: data.root, nodes: data.nodes });
			json(res, 404, { error: "not found" });
		} catch (e) {
			json(res, 500, { error: e.message });
		}
	};
}
