// Small client for the local T3 Code server. It follows the approach of T3 Tots' t3ctl and t3listen.
// The token is read in-process from T3_TOTS_DIR/token and only ever placed in the Authorization header.
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

function t3Dir() {
	for (let v of [process.env.T3_HOME, process.env.T3CODE_HOME]) {
		v = (v || "").trim();
		if (!v) continue;
		return fs.existsSync(path.join(v, "server-runtime.json")) ? v : path.join(v, "userdata");
	}
	return path.join(os.homedir(), ".t3", "userdata");
}

export function connect() {
	const tots = process.env.T3_TOTS_DIR;
	if (!tots) throw new Error("set T3_TOTS_DIR to the T3 Tots folder that holds the token");
	const rt = JSON.parse(fs.readFileSync(path.join(t3Dir(), "server-runtime.json"), "utf8"));
	const host = !rt.host || rt.host === "0.0.0.0" || rt.host === "::" ? "127.0.0.1" : rt.host;
	const base = `http://${host}:${rt.port}`;
	const headers = {
		Authorization: "Bearer " + fs.readFileSync(path.join(tots, "token"), "utf8").trim(),
		"x-t3-orchestration-protocol": "2",
	};
	const wsUrl = `${base.replace("http", "ws")}/ws?orchestrationProtocol=2`;

	async function get(p) {
		const r = await fetch(base + p, { headers });
		if (!r.ok) throw new Error(`${p} -> HTTP ${r.status}`);
		return r.json();
	}

	function rpc(tag, payload) {
		return new Promise((resolve, reject) => {
			const ws = new WebSocket(wsUrl, { headers });
			const timer = setTimeout(() => {
				ws.close();
				reject(new Error(`rpc ${tag} timeout`));
			}, 60000);
			ws.onopen = () => ws.send(JSON.stringify({ _tag: "Request", id: "1", tag, payload, headers: [] }));
			ws.onmessage = (ev) => {
				for (const m of [JSON.parse(ev.data)].flat()) {
					if (m._tag === "Ping") ws.send(JSON.stringify({ _tag: "Pong" }));
					if (m._tag === "Exit" && m.requestId === "1") {
						clearTimeout(timer);
						ws.close();
						if (m.exit._tag === "Success") resolve(m.exit.value);
						else reject(new Error(`rpc ${tag} failed: ${JSON.stringify(m.exit.cause || m.exit).slice(0, 600)}`));
					}
					if (m._tag === "Defect") {
						clearTimeout(timer);
						ws.close();
						reject(new Error(`rpc ${tag} defect`));
					}
				}
			};
			ws.onerror = () => {
				clearTimeout(timer);
				reject(new Error(`rpc ${tag}: T3 not reachable or token rejected`));
			};
		});
	}

	function followShell(onItem) {
		return new Promise((resolve) => {
			const ws = new WebSocket(wsUrl, { headers });
			ws.onopen = () => ws.send(JSON.stringify({ _tag: "Request", id: "shell", tag: "orchestration.subscribeShell", payload: {}, headers: [] }));
			ws.onmessage = (ev) => {
				for (const m of [JSON.parse(ev.data)].flat()) {
					if (m._tag === "Ping") ws.send(JSON.stringify({ _tag: "Pong" }));
					if (m._tag !== "Chunk") continue;
					ws.send(JSON.stringify({ _tag: "Ack", requestId: m.requestId }));
					for (const v of m.values || []) onItem(v);
				}
			};
			ws.onerror = () => {};
			ws.onclose = () => resolve();
			followShell.close = () => ws.close();
		});
	}

	const dispatch = (type, threadId, extra = {}) =>
		rpc("orchestration.dispatchCommand", { type, commandId: crypto.randomUUID(), threadId, ...extra });

	return {
		shell: () => get("/api/orchestration/shell"),
		thread: (id) => get(`/api/orchestration/threads/${encodeURIComponent(id)}`),
		followShell,
		async launch({ projectId, title, prompt, modelSelection }) {
			const res = await rpc("orchestration.launchThread", {
				commandId: crypto.randomUUID(),
				creationSource: "web",
				projectId,
				title,
				generateTitle: false,
				modelSelection,
				runtimeMode: "full-access",
				interactionMode: "default",
				workspaceStrategy: { type: "root" },
				initialMessage: { messageId: crypto.randomUUID(), text: prompt, attachments: [] },
			});
			return res.threadId;
		},
		// Starts a turn in the thread, or queues one behind its active run. Returns the message id.
		async send(t, text) {
			const messageId = crypto.randomUUID();
			await rpc("orchestration.dispatchCommand", {
				type: "message.dispatch",
				createdBy: "user",
				creationSource: "web",
				commandId: crypto.randomUUID(),
				threadId: t.id,
				messageId,
				text,
				attachments: [],
				dispatchMode: t.activeRunId ? { type: "queue_after_active" } : { type: "start_immediately" },
			});
			return messageId;
		},
		archive: (id) => dispatch("thread.archive", id),
	};
}

export function runOf(snap, messageId) {
	return ((snap.projection || snap).runs || []).find((r) => r.userMessageId === messageId) || null;
}

export function lastAssistantText(snap) {
	const items = (snap.projection || snap).turnItems || [];
	const list = Array.isArray(items) ? items : Object.values(items);
	const last = list.filter((i) => i.type === "assistant_message" && i.text).at(-1);
	return last ? String(last.text) : "";
}
