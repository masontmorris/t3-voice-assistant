// The bot's localhost API. serve.mjs mounts it on 127.0.0.1. See serve.mjs for the routes.
// Binding to localhost is not enough: any web page in a browser on this machine can send requests there. So every route
// refuses a request that carries an Origin header (browsers send one on cross-site requests), requires the shared secret in
// the x-bot-secret header (a page cannot set a custom header without a preflight this server never answers), and every POST
// must be application/json.
import crypto from "node:crypto";
import { ack, handoff, snapshot } from "./root.mjs";

export const SECRET_HEADER = "x-bot-secret";
const MAX_BODY = 1 << 20;

const sameSecret = (given, secret) => {
	const a = Buffer.from(String(given || ""));
	const b = Buffer.from(secret);
	return a.length === b.length && crypto.timingSafeEqual(a, b);
};

// The reason a request is refused, or null. Exported for the tests.
export function refusal(req, secret) {
	if (req.headers.origin !== undefined) return [403, "requests with an Origin header are refused"];
	if (!secret || !sameSecret(req.headers[SECRET_HEADER], secret)) return [401, "missing or wrong secret"];
	if (req.method === "POST" && (req.headers["content-type"] || "").split(";")[0].trim().toLowerCase() !== "application/json") return [415, "POST bodies must be application/json"];
	return null;
}

// deps: { data, save, t3, waiters: Set<() => void>, say, secret }
export function createApiHandler({ data, save, t3, waiters, say, secret }) {
	const json = (res, code, body) => {
		res.writeHead(code, { "content-type": "application/json" });
		res.end(JSON.stringify(body));
	};
	const body = (req) =>
		new Promise((resolve, reject) => {
			let s = "";
			req.on("data", (c) => {
				s += c;
				if (s.length > MAX_BODY) reject(Object.assign(new Error("body too large"), { code: 413 }));
			});
			req.on("end", () => {
				try {
					resolve(s ? JSON.parse(s) : {});
				} catch {
					reject(Object.assign(new Error("body is not JSON"), { code: 400 }));
				}
			});
		});
	const after = (id) => data.outbox.slice(id ? data.outbox.findIndex((e) => e.id === id) + 1 : 0);

	return async (req, res) => {
		try {
			const refused = refusal(req, secret);
			if (refused) {
				req.resume();
				return json(res, refused[0], { error: refused[1] });
			}
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
			json(res, e.code === 400 || e.code === 413 ? e.code : 500, { error: e.message });
		}
	};
}
