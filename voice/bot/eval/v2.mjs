// v2 of the gate's verdict question, written from the one v1 dev miss (s1: quiet for 45 minutes while "running", with a calm
// "will check back" reply). It changes three things from v1 and leaves tell_mason alone:
//   1. quiet time is given in minutes, not seconds;
//   2. the instructions say to weigh quiet time, status and last reply together;
//   3. the stall criterion names long quiet and waiting on a background job, and the healthy criterion requires progress.
// Adoption rule, fixed before the held run: core.mjs adopts v2 only if, on the 12 held cases, v2 has fewer missed alarms than
// v1 (3) and at most 1 needless alert.
import { MODEL } from "../decider.mjs";

export function verdictRequest(worker, pm, trigger, quietSec, lastReply) {
	return {
		model: MODEL,
		state: {
			worker: { role: worker.role, charter: worker.charter, status: worker.status, quiet_minutes: Math.round(quietSec / 6) / 10, last_reply: lastReply.slice(0, 300) },
			pm: { role: pm.role, charter: pm.charter },
			trigger: trigger.kind,
			delta: trigger.delta,
		},
		questions: {
			verdict: {
				type: "choice",
				instructions:
					"A heartbeat checks on a worker agent thread that reports to a PM thread. Decide what to do about this worker now. Judge quiet_minutes, status and last_reply together: a calm last reply does not mean the worker is fine if it has been quiet for a long time.",
				criteria: {
					stay_quiet: "The worker is making progress on its charter, or it finished and needs nothing from anyone. Nobody needs to look.",
					wake_pm: "The worker has stalled: it has been quiet far longer than its current step should take, or it is waiting on a background job, a build, a download or a retry loop with no progress. Or it drifted from its charter, or it is waiting on an answer from its PM that nobody has given. The PM should look now.",
					tell_mason: "Only the human owner can unblock it: it needs money, an account, a credential, or a decision outside the PM's charter.",
				},
			},
		},
	};
}
