// Step 0: can a client outside T3, holding only the T3 Tots token, start a new turn in an existing thread?
// Usage: node step0.mjs <projectId>. Creates one throwaway thread on the cheapest model, then sends it a second message.
import { connect, lastAssistantText, runOf } from "./t3.mjs";

const projectId = process.argv[2];
if (!projectId) throw new Error("usage: node step0.mjs <projectId>");
const t3 = connect();
const stamp = () => new Date().toISOString();

// Resolves with the thread once the stream shows it idle with a completed latest run newer than `after`.
function idleAfter(threadId, after) {
	return new Promise((resolve, reject) => {
		const timer = setTimeout(() => reject(new Error(`thread ${threadId} not idle after 180 s`)), 180000);
		t3.followShell((v) => {
			const threads = v.kind === "snapshot" ? v.snapshot.threads : v.kind === "thread.updated" ? [v.thread] : [];
			const t = threads.find((x) => x.id === threadId);
			if (t && !t.activeRunId && t.latestRunCompletedAt && t.latestRunCompletedAt > after) {
				clearTimeout(timer);
				t3.followShell.close();
				resolve(t);
			}
		});
	});
}

const t0 = stamp();
const threadId = await t3.launch({
	projectId,
	title: "bot-spike step0 (throwaway)",
	prompt: "Throwaway test thread. Reply with one short line: ok.",
	modelSelection: { instanceId: "claudeAgent", model: "claude-haiku-5-5", options: [{ id: "effort", value: "low" }] },
});
console.log(`${t0} launched ${threadId}`);
const first = await idleAfter(threadId, t0);
console.log(`${stamp()} first run ${first.latestRunId} done, status ${first.status}`);

const t1 = stamp();
const messageId = await t3.send(first, "Second message from outside T3. Reply with one short line: woken.");
console.log(`${t1} sent message ${messageId}`);
const second = await idleAfter(threadId, t1);
const snap = await t3.thread(threadId);
const run = runOf(snap, messageId);
console.log(`${stamp()} second run ${run && run.id} status ${run && run.status}; latestRunId ${second.latestRunId}`);
console.log(`new turn started from outside: ${!!run && run.id !== first.latestRunId}`);
console.log(`last reply: ${lastAssistantText(snap).slice(0, 80)}`);
