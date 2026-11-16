import assert from "node:assert/strict";
import http from "node:http";
import { after, before, test } from "node:test";
import { createApiHandler } from "../api.mjs";
import { emptyTree } from "../root.mjs";

const SECRET = "test-secret-0123456789abcdef";
let server;
let base;
const sent = [];
const data = { ...emptyTree(), root: "R" };
const t3 = {
	async shell() {
		return { threads: [{ id: "R", activeRunId: null }] };
	},
	async send(t, text) {
		sent.push(text);
		return `m${sent.length}`;
	},
	async thread() {
		return { turnItems: [], runs: [] };
	},
};

before(async () => {
	server = http.createServer(createApiHandler({ data, save: () => {}, t3, waiters: new Set(), say: () => {}, secret: SECRET }));
	await new Promise((r) => server.listen(0, "127.0.0.1", r));
	base = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());

const post = (path, body, headers) => fetch(base + path, { method: "POST", body: JSON.stringify(body), headers });
const good = { "content-type": "application/json", "x-bot-secret": SECRET };

test("a request without the secret is refused on every route, and the root gets nothing", async () => {
	for (const path of ["/registry", "/outbox", "/snapshot?conversation=c"]) assert.equal((await fetch(base + path)).status, 401, path);
	assert.equal((await post("/handoff", { requestId: "a", text: "x" }, { "content-type": "application/json" })).status, 401);
	assert.equal((await fetch(base + "/registry", { headers: { "x-bot-secret": "wrong" } })).status, 401, "a wrong secret");
	assert.equal(sent.length, 0);
});

test("a request with an Origin header is refused, even with the secret", async () => {
	assert.equal((await fetch(base + "/registry", { headers: { "x-bot-secret": SECRET, origin: "https://example.com" } })).status, 403);
	assert.equal((await post("/handoff", { requestId: "b", text: "x" }, { ...good, origin: "null" })).status, 403);
	assert.equal(sent.length, 0);
});

test("a POST that is not application/json is refused, including a browser's no-preflight text/plain", async () => {
	assert.equal((await post("/handoff", { requestId: "c", text: "x" }, { "x-bot-secret": SECRET, "content-type": "text/plain" })).status, 415);
	assert.equal((await post("/outbox/ack", { ids: [] }, { "x-bot-secret": SECRET, "content-type": "application/x-www-form-urlencoded" })).status, 415);
	assert.equal(sent.length, 0);
});

test("a correct request still works", async () => {
	const r = await post("/handoff", { requestId: "d", text: "drive time" }, good);
	assert.equal(r.status, 200);
	assert.equal((await r.json()).status, "created");
	assert.equal(sent.length, 1);
	assert.equal((await fetch(base + "/registry", { headers: { "x-bot-secret": SECRET } })).status, 200);
	assert.equal((await post("/handoff", { requestId: "e", text: "x" }, { ...good, "content-type": "application/json; charset=utf-8" })).status, 200, "a charset parameter is fine");
});
