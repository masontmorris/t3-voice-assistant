"""Hand labels for Mason's recorded turns, read from the transcripts and the live-call log. Subjective and small.

request: asks the assistant to do or look something up. correction: restates a wanted value against a reply.
conversation: everything else (reaction, remark about the assistant, thanks, mic check). None means too ambiguous to
score: 18 ("The word hello.") restates a request, 23 and 29 are challenges that were also read as requests.
"""
KIND = {
    "a001": "conversation", "a002": "request", "a003": "conversation", "a004": "request", "a005": "conversation",
    "a006": "conversation", "a007": "conversation", "a010": "conversation", "a011": "conversation",
    "a012": "conversation", "a013": "conversation", "a014": "conversation", "a015": "request", "a016": "request",
    "a017": "request", "a018": None, "a019": "conversation", "a020": "request", "a021": "request", "a022": "request",
    "a023": None, "a024": "conversation", "a025": "request", "a026": "correction", "a027": "conversation",
    "a028": "request", "a029": None, "a030": "conversation", "a031": "conversation", "a032": "request",
    "a033": "conversation", "a034": "conversation", "a035": "conversation", "a036": "conversation",
    "a037": "conversation",
    "b002": "conversation", "b004": "conversation", "b006": "conversation",
}
