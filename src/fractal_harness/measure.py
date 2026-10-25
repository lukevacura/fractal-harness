"""What the harness injects, measured without a model: how big it is, and how much of it two prompts share.

Both questions are about text the hooks produce, so both can be answered by calling the hooks. They are the
inputs to token accounting (what the injection costs).

A caution on the shared start. A prompt cache reuses the longest identical *start of a request*. In recorded
Claude Code sessions (the transcripts under ~/.claude/projects) the prompt hook's text and a subagent's
start-hook text are attachments that come *after* the user's prompt, so the request's start is the varying
prompt and a static start of the injected text is not a cache prefix. `shared_start_*` therefore measures
how much two injections have in common, not how much a cache would reuse. It would predict reuse only for
text placed before the prompt, which these hooks cannot do.

Token counts here are estimates (characters / 4); the exact count needs the model's tokenizer, and only
ratios between runs of the same estimate are meaningful.
"""

from __future__ import annotations

from math import ceil
from pathlib import Path

from .hook import _has_graph, _has_store, context_for

CHARS_PER_TOKEN = 4


def tokens_estimate(chars: int) -> int:
    return ceil(chars / CHARS_PER_TOKEN)


def common_prefix(texts: list[str]) -> int:
    """Length of the longest start every text has."""
    if not texts:
        return 0
    shortest = min(texts, key=len)
    n = 0
    for i, ch in enumerate(shortest):
        if any(t[i] != ch for t in texts):
            break
        n = i + 1
    return n


def neighbour_sizes(root: Path, prompt: str, capacity: int = 1000) -> dict[str, int]:
    """For the task map of `prompt`: the characters of neighbour signatures offered, against the characters of
    the neighbour files they stand in for (what reading them would cost). Empty when there is no map."""
    from .taskmap import task_map
    m = task_map(root, prompt, capacity)
    sig = src = 0
    for reg in m.regions:
        for up, sigs in reg.interfaces.items():
            sig += sum(len(s) for s in sigs)
            try:
                src += len((root / up).read_text(errors="replace"))
            except OSError:
                pass
    return {"signature_chars": sig, "neighbour_source_chars": src}


def stats(root: Path, prompts: list[str], capacity: int = 1000) -> dict:
    """Size of the injection for each prompt, the start the injections share, and whether asking twice
    gives the same text. Reads the store; writes no telemetry."""
    root = Path(root)
    texts = [context_for(root, p, log=False) or "" for p in prompts]
    again = [context_for(root, p, log=False) or "" for p in prompts]
    hits = [t for t in texts if t]
    shared = common_prefix(hits) if len(hits) > 1 else 0
    out = {
        "injections": [{"prompt": p, "chars": len(t), "tokens_estimate": tokens_estimate(len(t)), "injected": bool(t)}
                       for p, t in zip(prompts, texts)],
        "shared_start_chars": shared,
        "shared_start_share": round(shared / min(len(t) for t in hits), 3) if len(hits) > 1 else 0.0,
        "repeatable": texts == again,
    }
    sizes = [neighbour_sizes(root, p, capacity) for p in prompts] if _has_store(root) and _has_graph(root) else []
    out["neighbours"] = {k: sum(s[k] for s in sizes) for k in ("signature_chars", "neighbour_source_chars")}
    return out
