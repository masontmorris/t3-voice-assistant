"""Voice domain types for the T3 seam.

Every frame the seam sends decodes into one of these frozen dataclasses, and
every way a socket ends reports as one SocketEnd value. Nothing here names a
T3 wire type. The schema test checks these against voice/t3/seam.schema.json.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, Union


class RefusalCode(str, Enum):
    """One member per code in the schema's RefusalCode enum."""

    OPERATION_NOT_ALLOWED = "operation_not_allowed"
    PROJECT_NOT_ALLOWED = "project_not_allowed"
    ABOVE_CEILING = "above_ceiling"
    THREAD_BUSY = "thread_busy"
    NOT_FOUND = "not_found"
    GONE = "gone"
    REFUSED = "refused"
    TOO_MANY_WATCHES = "too_many_watches"
    NO_DEFAULT_MODEL = "no_default_model"
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "unavailable"


class WatchEndReason(str, Enum):
    """One member per reason in the schema's WatchEndReason enum."""

    GONE = "gone"
    RESYNC = "resync"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


class ThreadActivity(str, Enum):
    """How a thread reads, from the shell row alone."""

    WORKING = "working"
    WAITING_ON_YOU = "waiting_on_you"
    WAITING_ON_BACKGROUND = "waiting_on_background"
    QUEUED = "queued"
    IDLE = "idle"
    FINISHED = "finished"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"


class RunStatus(str, Enum):
    """All ten run statuses, by name."""

    PREPARING = "preparing"
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"


class TerminalStatus(str, Enum):
    """The five statuses isTerminalRunStatus accepts."""

    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"


class PendingKind(str, Enum):
    """Kinds a needs_you carries."""

    APPROVAL = "approval"
    QUESTION = "question"
    IN_APP = "in_app"


class NeedSource(str, Enum):
    """What raised a need: a runtime request kind, a link or secret card, or a card only the shell names."""

    COMMAND = "command"
    FILE_READ = "file-read"
    FILE_CHANGE = "file-change"
    MCP_ELICITATION = "mcp-elicitation"
    PERMISSION = "permission"
    DYNAMIC_TOOL_CALL = "dynamic_tool_call"
    USER_INPUT = "user_input"
    AUTH_REFRESH = "auth_refresh"
    LINK_REQUEST = "link_request"
    SECRET_REQUEST = "secret_request"
    CARD = "card"


class ResolvedStatus(str, Enum):
    """Statuses a resolved event carries."""

    RESOLVED = "resolved"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class RuntimeMode(str, Enum):
    """Ceiling runtime modes hello carries."""

    APPROVAL_REQUIRED = "approval-required"
    AUTO_ACCEPT_EDITS = "auto-accept-edits"
    AUTO = "auto"
    FULL_ACCESS = "full-access"


class Delivery(str, Enum):
    """How a send reached its thread. A voice send never steers."""

    STARTED = "started"
    QUEUED = "queued"


class Operation(str, Enum):
    """Ceiling operations, the short names hello carries."""

    LIST = "list"
    READ = "read"
    WATCH = "watch"
    LAUNCH = "launch"
    SEND = "send"
    INTERRUPT = "interrupt"
    RESPOND = "respond"
    ANSWER = "answer"


WIRE_OPS: tuple[str, ...] = (
    "list_threads",
    "read_thread",
    "watch",
    "unwatch",
    "launch",
    "send",
    "interrupt",
    "respond",
    "answer_question",
)


@dataclass(frozen=True)
class Ceiling:
    max_runtime_mode: RuntimeMode
    interaction_mode: str
    operations: tuple[Operation, ...]
    max_watches: int


@dataclass(frozen=True)
class Project:
    project_id: str
    name: str
    spoken_names: tuple[str, ...]


@dataclass(frozen=True)
class Hello:
    ceiling: Ceiling
    projects: tuple[Project, ...]
    latest_sequence: int
    # The T3 environment the seam serves; None from a server that does not say.
    environment_id: str | None = None


@dataclass(frozen=True)
class ThreadSummary:
    thread_id: str
    project_id: str
    title: str
    activity: ThreadActivity
    last_activity_at: str
    pinned: bool


@dataclass(frozen=True)
class ThreadList:
    threads: tuple[ThreadSummary, ...]
    total: int
    truncated: bool
    as_of_sequence: int


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    status: RunStatus
    started_at: str | None


@dataclass(frozen=True)
class QuestionOption:
    label: str
    # Context for choosing the option, never said aloud.
    description: str


@dataclass(frozen=True)
class Question:
    """One question of a request the voice can ask and answer. Absent from a need the voice cannot answer."""

    id: str
    question: str
    options: tuple[QuestionOption, ...]
    # Whether the caller's own words are an answer when no option fits.
    own_words: bool


@dataclass(frozen=True)
class PendingNeed:
    request_id: str
    kind: PendingKind
    prompt: str
    source: NeedSource
    questions: tuple[Question, ...] = ()


@dataclass(frozen=True)
class RunResult:
    text: str
    message_id: str | None
    turn_item_id: str | None


@dataclass(frozen=True)
class LastReply:
    text: str
    message_id: str | None
    turn_item_id: str | None
    run_id: str
    at: str


@dataclass(frozen=True)
class ThreadState:
    thread_id: str
    project_id: str
    title: str
    activity: ThreadActivity
    run: RunInfo | None
    pending: PendingNeed | None
    last_reply: LastReply | None
    as_of_sequence: int
    # Whether T3 stored what the pending request asks. When false its prompt is a fixed line about the kind of request,
    # which cannot tell one command from another, so the voice does not approve it.
    pending_detailed: bool = True


@dataclass(frozen=True)
class WatchStarted:
    thread_id: str


@dataclass(frozen=True)
class LaunchResult:
    """A launch, fresh or replayed. When replayed, read the thread before watching it."""

    thread_id: str
    run_id: str | None
    runtime_mode: RuntimeMode
    model: str
    sequence: int
    replayed: bool


@dataclass(frozen=True)
class SendResult:
    """A send, fresh or replayed. Read the thread before watching it either way."""

    thread_id: str
    message_id: str
    run_id: str
    delivery: Delivery
    sequence: int
    replayed: bool


@dataclass(frozen=True)
class InterruptRequested:
    run_id: str
    sequence: int


@dataclass(frozen=True)
class NothingRunning:
    """No run was active, or this request id already interrupted one. Report the first result."""


InterruptResult = Union[InterruptRequested, NothingRunning]


@dataclass(frozen=True)
class RespondResult:
    """An answer to one pending approval, fresh or replayed. `decision` is what the voice sent: approve or deny."""

    thread_id: str
    pending_request_id: str
    decision: str
    sequence: int
    replayed: bool

@dataclass(frozen=True)
class RecordedAnswer:
    question_id: str
    answer: str


@dataclass(frozen=True)
class AnswerResult:
    """An answer to one pending question, fresh or replayed. `answers` is what T3 recorded, in the request's order."""

    thread_id: str
    pending_request_id: str
    answers: tuple[RecordedAnswer, ...]
    sequence: int
    replayed: bool


INTERRUPT_KINDS: dict[str, type] = {
    "requested": InterruptRequested,
    "nothing_running": NothingRunning,
}


@dataclass(frozen=True)
class RunEvent:
    thread_id: str
    run_id: str
    status: RunStatus
    sequence: int


@dataclass(frozen=True)
class ReplyEvent:
    thread_id: str
    run_id: str | None
    message_id: str
    text: str
    sequence: int


@dataclass(frozen=True)
class NeedsYouEvent:
    thread_id: str
    sequence: int
    request_id: str
    kind: PendingKind
    prompt: str
    source: NeedSource
    questions: tuple[Question, ...] = ()


@dataclass(frozen=True)
class ResolvedEvent:
    thread_id: str
    request_id: str
    status: ResolvedStatus
    sequence: int


@dataclass(frozen=True)
class DoneEvent:
    thread_id: str
    run_id: str
    status: TerminalStatus
    result: RunResult
    sequence: int
    as_of_sequence: int


@dataclass(frozen=True)
class GoneEvent:
    thread_id: str
    sequence: int


VoiceEvent = Union[RunEvent, ReplyEvent, NeedsYouEvent, ResolvedEvent, DoneEvent, GoneEvent]

EVENT_TYPES: dict[str, type] = {
    "run": RunEvent,
    "reply": ReplyEvent,
    "needs_you": NeedsYouEvent,
    "resolved": ResolvedEvent,
    "done": DoneEvent,
    "gone": GoneEvent,
}


@dataclass(frozen=True)
class WatchEnded:
    thread_id: str
    reason: WatchEndReason


@dataclass(frozen=True)
class ResultErr:
    code: RefusalCode
    message: str
    reason: str | None = None
    command_id: str | None = None
    thread_id: str | None = None

    def spoken(self) -> str:
        """The server's message is short text the voice may speak."""
        return self.message


@dataclass(frozen=True)
class Close1013:
    label: str = field(default="close-1013", init=False)


@dataclass(frozen=True)
class Close1008:
    label: str = field(default="close-1008", init=False)


@dataclass(frozen=True)
class Close1007:
    label: str = field(default="close-1007", init=False)


@dataclass(frozen=True)
class Close1011:
    label: str = field(default="close-1011", init=False)


@dataclass(frozen=True)
class CloseOther:
    code: int
    label: str = field(default="close-other", init=False)


@dataclass(frozen=True)
class Upgrade401:
    label: str = field(default="upgrade-401", init=False)


@dataclass(frozen=True)
class Upgrade403:
    label: str = field(default="upgrade-403", init=False)


@dataclass(frozen=True)
class Upgrade404:
    label: str = field(default="upgrade-404", init=False)


@dataclass(frozen=True)
class Unreachable:
    label: str = field(default="unreachable", init=False)


SocketEnd = Union[
    Close1013, Close1008, Close1007, Close1011, CloseOther,
    Upgrade401, Upgrade403, Upgrade404, Unreachable,
]

CLOSE_1013 = Close1013()
CLOSE_1008 = Close1008()
CLOSE_1007 = Close1007()
CLOSE_1011 = Close1011()
UPGRADE_401 = Upgrade401()
UPGRADE_403 = Upgrade403()
UPGRADE_404 = Upgrade404()
UNREACHABLE = Unreachable()


def decode_close(code: int | None) -> SocketEnd:
    """Map a socket close code to its typed value. None means no close frame."""
    if code == 1013:
        return CLOSE_1013
    if code == 1008:
        return CLOSE_1008
    if code == 1007:
        return CLOSE_1007
    if code == 1011:
        return CLOSE_1011
    return CloseOther(code=code if code is not None else 1006)


def _need(obj: Any, name: str) -> Any:
    if not isinstance(obj, dict) or name not in obj:
        raise ValueError(f"seam frame is missing {name!r}: {obj!r}")
    return obj[name]


def decode_hello(obj: Any) -> Hello:
    ceiling = _need(obj, "ceiling")
    return Hello(
        ceiling=Ceiling(
            max_runtime_mode=RuntimeMode(_need(ceiling, "maxRuntimeMode")),
            interaction_mode=_need(ceiling, "interactionMode"),
            operations=tuple(Operation(op) for op in _need(ceiling, "operations")),
            max_watches=_need(ceiling, "maxWatches"),
        ),
        projects=tuple(
            Project(
                project_id=_need(p, "projectId"),
                name=_need(p, "name"),
                spoken_names=tuple(_need(p, "spokenNames")),
            )
            for p in _need(obj, "projects")
        ),
        latest_sequence=_need(obj, "latestSequence"),
        environment_id=obj.get("environmentId"),
    )


def decode_thread_list(value: Any) -> ThreadList:
    return ThreadList(
        threads=tuple(
            ThreadSummary(
                thread_id=_need(t, "threadId"),
                project_id=_need(t, "projectId"),
                title=_need(t, "title"),
                activity=ThreadActivity(_need(t, "activity")),
                last_activity_at=_need(t, "lastActivityAt"),
                pinned=_need(t, "pinned"),
            )
            for t in _need(value, "threads")
        ),
        total=_need(value, "total"),
        truncated=_need(value, "truncated"),
        as_of_sequence=_need(value, "asOfSequence"),
    )


def _decode_run(raw: Any) -> RunInfo | None:
    if raw is None:
        return None
    return RunInfo(
        run_id=_need(raw, "runId"),
        status=RunStatus(_need(raw, "status")),
        started_at=raw.get("startedAt"),
    )


def _decode_questions(raw: Any) -> tuple[Question, ...]:
    return tuple(
        Question(
            id=_need(q, "id"),
            question=_need(q, "question"),
            options=tuple(
                QuestionOption(label=_need(o, "label"), description=_need(o, "description"))
                for o in _need(q, "options")
            ),
            own_words=_need(q, "ownWords"),
        )
        for q in raw or ()
    )


def _decode_pending(raw: Any) -> PendingNeed | None:
    if raw is None:
        return None
    return PendingNeed(
        request_id=_need(raw, "requestId"),
        kind=PendingKind(_need(raw, "kind")),
        prompt=_need(raw, "prompt"),
        source=NeedSource(_need(raw, "source")),
        questions=_decode_questions(raw.get("questions")),
    )


def _decode_last_reply(raw: Any) -> LastReply | None:
    if raw is None:
        return None
    return LastReply(
        text=_need(raw, "text"),
        message_id=raw.get("messageId"),
        turn_item_id=raw.get("turnItemId"),
        run_id=_need(raw, "runId"),
        at=_need(raw, "at"),
    )


def decode_thread_state(value: Any) -> ThreadState:
    return ThreadState(
        thread_id=_need(value, "threadId"),
        project_id=_need(value, "projectId"),
        title=_need(value, "title"),
        activity=ThreadActivity(_need(value, "activity")),
        run=_decode_run(value.get("run")),
        pending=_decode_pending(value.get("pending")),
        last_reply=_decode_last_reply(value.get("lastReply")),
        as_of_sequence=_need(value, "asOfSequence"),
        pending_detailed=value.get("pendingDetailed", False),
    )


def decode_event(obj: Any) -> VoiceEvent:
    """Decode one VoiceEvent. A done with empty text passes through unchanged."""
    kind = _need(obj, "type")
    if kind == "run":
        return RunEvent(
            thread_id=_need(obj, "threadId"),
            run_id=_need(obj, "runId"),
            status=RunStatus(_need(obj, "status")),
            sequence=_need(obj, "sequence"),
        )
    if kind == "reply":
        return ReplyEvent(
            thread_id=_need(obj, "threadId"),
            run_id=obj.get("runId"),
            message_id=_need(obj, "messageId"),
            text=_need(obj, "text"),
            sequence=_need(obj, "sequence"),
        )
    if kind == "needs_you":
        return NeedsYouEvent(
            thread_id=_need(obj, "threadId"),
            sequence=_need(obj, "sequence"),
            request_id=_need(obj, "requestId"),
            kind=PendingKind(_need(obj, "kind")),
            prompt=_need(obj, "prompt"),
            source=NeedSource(_need(obj, "source")),
            questions=_decode_questions(obj.get("questions")),
        )
    if kind == "resolved":
        return ResolvedEvent(
            thread_id=_need(obj, "threadId"),
            request_id=_need(obj, "requestId"),
            status=ResolvedStatus(_need(obj, "status")),
            sequence=_need(obj, "sequence"),
        )
    if kind == "done":
        result = _need(obj, "result")
        return DoneEvent(
            thread_id=_need(obj, "threadId"),
            run_id=_need(obj, "runId"),
            status=TerminalStatus(_need(obj, "status")),
            result=RunResult(
                text=_need(result, "text"),
                message_id=result.get("messageId"),
                turn_item_id=result.get("turnItemId"),
            ),
            sequence=_need(obj, "sequence"),
            as_of_sequence=_need(obj, "asOfSequence"),
        )
    if kind == "gone":
        return GoneEvent(thread_id=_need(obj, "threadId"), sequence=_need(obj, "sequence"))
    raise ValueError(f"unknown seam event type: {kind!r}")


def decode_watch_ended(obj: Any) -> WatchEnded:
    return WatchEnded(
        thread_id=_need(obj, "threadId"),
        reason=WatchEndReason(_need(obj, "reason")),
    )


def decode_launch_result(value: Any) -> LaunchResult:
    return LaunchResult(
        thread_id=_need(value, "threadId"),
        run_id=_need(value, "runId"),
        runtime_mode=RuntimeMode(_need(value, "runtimeMode")),
        model=_need(value, "model"),
        sequence=_need(value, "sequence"),
        replayed=_need(value, "replayed"),
    )


def decode_send_result(value: Any) -> SendResult:
    return SendResult(
        thread_id=_need(value, "threadId"),
        message_id=_need(value, "messageId"),
        run_id=_need(value, "runId"),
        delivery=Delivery(_need(value, "delivery")),
        sequence=_need(value, "sequence"),
        replayed=_need(value, "replayed"),
    )


def decode_interrupt_result(value: Any) -> InterruptResult:
    kind = _need(value, "kind")
    if kind == "requested":
        return InterruptRequested(run_id=_need(value, "runId"), sequence=_need(value, "sequence"))
    if kind == "nothing_running":
        return NothingRunning()
    raise ValueError(f"unknown interrupt result kind: {kind!r}")


def decode_respond_result(value: Any) -> RespondResult:
    return RespondResult(
        thread_id=_need(value, "threadId"),
        pending_request_id=_need(value, "pendingRequestId"),
        decision=_need(value, "decision"),
        sequence=_need(value, "sequence"),
        replayed=_need(value, "replayed"),
    )


def decode_answer_result(value: Any) -> AnswerResult:
    return AnswerResult(
        thread_id=_need(value, "threadId"),
        pending_request_id=_need(value, "pendingRequestId"),
        answers=tuple(
            RecordedAnswer(question_id=_need(a, "questionId"), answer=_need(a, "answer"))
            for a in _need(value, "answers")
        ),
        sequence=_need(value, "sequence"),
        replayed=_need(value, "replayed"),
    )


RESULT_DECODERS: dict[str, Callable[[Any], Any]] = {
    "list_threads": decode_thread_list,
    "read_thread": decode_thread_state,
    "watch": lambda value: WatchStarted(thread_id=_need(value, "threadId")),
    "unwatch": lambda value: WatchStarted(thread_id=_need(value, "threadId")),
    "launch": decode_launch_result,
    "send": decode_send_result,
    "interrupt": decode_interrupt_result,
    "respond": decode_respond_result,
    "answer_question": decode_answer_result,
}

ResultValue = Union[
    ThreadList, ThreadState, WatchStarted, LaunchResult, SendResult, InterruptRequested, NothingRunning,
    RespondResult, AnswerResult,
]

DrainItem = Union[VoiceEvent, WatchEnded, SocketEnd]
