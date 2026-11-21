import { useAtomValue } from "@effect/atom-react";
import { scopeThreadRef } from "@t3tools/client-runtime/environment";
import type { OrchestrationV2VoiceCallLine } from "@t3tools/contracts";
import { useGT } from "gt-react";
import { AudioLinesIcon, ChevronDownIcon } from "lucide-react";
import { use, useState } from "react";

import { cn } from "~/lib/utils";
import { environmentThreadDetails } from "../../state/threads";
import { Button } from "../ui/button";
import { Collapsible, CollapsiblePanel, CollapsibleTrigger } from "../ui/collapsible";
import { TimelineRowCtx } from "./timelineRowContext";
import {
  resolveVoiceCallEvidence,
  shownVoiceCallLines,
  type VoiceCallItem,
  voiceCallMinutes,
} from "./VoiceCallCard.logic";
import { WorkLogBlock } from "./WorkLog";

/** A finished voice call on the thread, collapsed to one line like a subagent's card until opened. */
export function VoiceCallCard(props: {
  readonly rowId: string;
  readonly item: VoiceCallItem;
  readonly continues?: boolean | undefined;
}) {
  const ctx = use(TimelineRowCtx);
  const gt = useGT();
  const viewKey = `voice-call:${props.item.id}`;
  const [expanded, setExpanded] = useState(() =>
    ctx.workGroupViewState.expandedEntries.has(viewKey),
  );
  const [showAll, setShowAll] = useState(false);
  const { item } = props;
  const label = gt("Voice call");
  const summary = gt(
    "{count, plural, one {{count} line} other {{count} lines}} · {minutes, plural, one {{minutes} min} other {{minutes} min}}",
    { count: item.lines.length, minutes: voiceCallMinutes(item) },
  );
  const toggle = (open: boolean) => {
    ctx.onToggleWorkEntry(props.rowId, expanded);
    if (open) ctx.workGroupViewState.expandedEntries.add(viewKey);
    else ctx.workGroupViewState.expandedEntries.delete(viewKey);
    setExpanded(open);
  };
  const lines = shownVoiceCallLines(item, showAll);
  return (
    <WorkLogBlock continues={props.continues}>
      <Collapsible open={expanded} onOpenChange={toggle} data-v2-item-type="voice_call">
        <CollapsibleTrigger
          aria-label={label}
          aria-description={summary}
          className={cn(
            "flex w-full min-w-0 items-center gap-2.5 py-2 pr-3.25 text-left transition-opacity hover:opacity-100",
            expanded ? "text-foreground opacity-100" : "text-muted-foreground opacity-55",
          )}
        >
          <span
            aria-hidden
            className="inline-flex size-6 shrink-0 items-center justify-center rounded-full border border-border/70 bg-muted"
          >
            <AudioLinesIcon className="size-3.5" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-xs font-semibold">{label}</span>
            <span className="block truncate text-3xs text-muted-foreground">{summary}</span>
          </span>
          <ChevronDownIcon
            aria-hidden
            className={cn("size-3.5 shrink-0 text-muted-foreground", expanded && "rotate-180")}
          />
        </CollapsibleTrigger>
        <CollapsiblePanel animate={false}>
          {expanded ? (
            <div className="mt-1 mb-1 flex flex-col gap-1.5 rounded-lg border border-border/60 bg-card/30 p-2">
              {lines.map(({ key, line }) => (
                <VoiceCallLineRow key={key} line={line} threadId={item.threadId} />
              ))}
              {lines.length < item.lines.length ? (
                <Button size="xs" variant="ghost" onClick={() => setShowAll(true)}>
                  {gt("Show all {count} lines", { count: item.lines.length })}
                </Button>
              ) : null}
              {item.truncated ? (
                <span className="text-3xs text-muted-foreground">
                  {gt("The rest of this call was too long to keep.")}
                </span>
              ) : null}
            </div>
          ) : null}
        </CollapsiblePanel>
      </Collapsible>
    </WorkLogBlock>
  );
}

function VoiceCallLineRow(props: {
  readonly line: OrchestrationV2VoiceCallLine;
  readonly threadId: VoiceCallItem["threadId"];
}) {
  const gt = useGT();
  const [showEvidence, setShowEvidence] = useState(false);
  const { line } = props;
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="flex items-baseline gap-2 text-2xs text-muted-foreground">
        <span className="font-medium">{line.speaker === "caller" ? gt("You") : gt("Voice")}</span>
        {line.relay ? <span className="text-info">{gt("about a thread")}</span> : null}
        {line.interrupted ? <span>{gt("cut off")}</span> : null}
        {line.claim !== undefined && line.claim.evidence.length > 0 ? (
          <Button
            size="xs"
            variant="ghost"
            aria-expanded={showEvidence}
            onClick={() => setShowEvidence((open) => !open)}
          >
            {gt("Evidence")}
          </Button>
        ) : null}
      </span>
      <span className={cn("text-xs whitespace-pre-wrap", line.relay && "text-info")}>
        {line.text}
      </span>
      {showEvidence && line.claim !== undefined ? (
        <VoiceCallEvidence evidence={line.claim.evidence} threadId={props.threadId} />
      ) : null}
    </div>
  );
}

function VoiceCallEvidence(props: {
  readonly evidence: NonNullable<OrchestrationV2VoiceCallLine["claim"]>["evidence"];
  readonly threadId: VoiceCallItem["threadId"];
}) {
  const ctx = use(TimelineRowCtx);
  const gt = useGT();
  const projection = useAtomValue(
    environmentThreadDetails.threadAtom(
      scopeThreadRef(ctx.activeThreadEnvironmentId, props.threadId),
    ),
    (thread) => thread?.projection,
  );
  return (
    <ul className="ms-2 flex flex-col gap-0.5 border-s border-border/60 ps-2 text-2xs text-muted-foreground">
      {props.evidence.map((evidence) => {
        const found = resolveVoiceCallEvidence(
          evidence,
          projection?.visibleTurnItems ?? [],
          projection?.runs ?? [],
        );
        return (
          <li key={evidence.sequence} className="truncate">
            {found.kind === "item"
              ? (found.text ?? gt("A thread item"))
              : found.kind === "run"
                ? gt("A run of the thread")
                : found.kind === "thread"
                  ? gt("The thread as it stood then")
                  : gt("Not in the loaded part of this thread")}
          </li>
        );
      })}
    </ul>
  );
}
