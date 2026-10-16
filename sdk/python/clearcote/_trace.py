"""Humanize score hook (SC 232 lane G): ``CLEARCOTE_HUMANIZE_SCORE=1`` makes the SDK record every
action it dispatches, score it with ``_motionscore.score_action`` and append one JSON line per action
to ``CLEARCOTE_HUMANIZE_SCORE_FILE`` (default ``./clearcote-humanize-score.jsonl``). A score below
``WARN_BELOW`` prints a ``clearcote: warning:`` line naming the penalties (once per kind + penalty set,
through ``_warnings.warn_once``).

Read-only tap: nothing here changes what is dispatched or when. ``record`` never raises -- a scoring
or file error is swallowed, so a humanize path can never fail because of its own instrumentation.

Who calls ``record``:
  * ``_pacing.walk`` / ``walk_async`` (this lane): every walked pointer path, kind ``glide`` (or the
    ``kind`` the caller passed: ``drag``, ``ambient``), samples ``{"x","y","t"}`` with the ACTUAL
    dispatch times (ms from the first dispatch), so the score reflects what the page saw, not the plan.
  * the typing loop (lane D): ``record("typed", [{"key","down","up"}, ...])`` once per typed run.
  * the wheel loop (lane E): ``record("wheel", [{"t","dy","dx","mode"}, ...])`` once per hwheel call.
  * hover dwell (lane C): ``record("hover", samples_from_arrival)``.
Sample shapes are documented at the top of the SC 232 block in ``_motionscore.py``.
"""
import json
import os
import threading
import time

ENV = "CLEARCOTE_HUMANIZE_SCORE"
FILE_ENV = "CLEARCOTE_HUMANIZE_SCORE_FILE"
DEFAULT_FILE = "./clearcote-humanize-score.jsonl"
WARN_BELOW = 0.7

_lock = threading.Lock()
_FALSE = ("", "0", "false", "off", "no")


def enabled():
    """True when the hook is on (``CLEARCOTE_HUMANIZE_SCORE`` set to anything but 0/false/off)."""
    return os.environ.get(ENV, "").strip().lower() not in _FALSE


def score_file():
    return os.environ.get(FILE_ENV) or DEFAULT_FILE


def _jsonable(v):
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def record(kind, samples, meta=None):
    """Score ``samples`` as ``kind`` and append the line; returns the ``ActionScore`` (None when the
    hook is off, the sample list is empty, or anything failed)."""
    if not enabled() or not samples:
        return None
    try:
        from ._motionscore import score_action
        r = score_action(kind, samples)
        line = {
            "t": round(time.time(), 3),
            "pid": os.getpid(),
            "kind": r.kind,
            "n": len(samples),
            "score": r.score,
            "flags": list(r.flags),
            "features": _jsonable(r.features),
        }
        if meta:
            line["meta"] = _jsonable(meta)
        with _lock:
            with open(score_file(), "a", encoding="utf-8") as f:
                f.write(json.dumps(line, separators=(",", ":"), default=str) + "\n")
        if r.score < WARN_BELOW:
            from ._warnings import warn_once
            names = ", ".join(x.split("(")[0] for x in r.flags) or "none"
            warn_once("humanize-score:%s:%s" % (r.kind, names),
                      "humanize %s scored %.2f (< %.1f): %s" % (r.kind, r.score, WARN_BELOW, names))
        return r
    except Exception:  # noqa: BLE001 - instrumentation must never break the action
        return None
