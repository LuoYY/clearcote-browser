"""Pure planning for humanized form input, shared by the sync and async humanize modules.

Nothing here touches a page: it decides WHAT a person would press for a form control, and the
humanize modules decide HOW (glides, holds, cadence). Keeping the decisions pure is what lets one
set of test vectors (sdk/testdata/humanize_forms_vectors.json) pin Python, Node (forms.ts) and
.NET (Forms.cs) to identical answers.

What a person does, measured on r37 headless (SC 197 forms probes, 2026-10-11):
  * <select> (a drop-down): a click opens the popup; letters typed while it is open move the
    popup's highlight and change NOTHING on the page; Enter commits -> exactly one trusted
    input + change. Typing on a focused but CLOSED select instead fires one change per letter, and
    stepping it with arrows fires one change per row -- the old route, which picked row 40 with 40
    change events.
  * list boxes (size > 1 or multiple): a click on a row selects it, Ctrl+click adds a row.
  * <input type=range>: a click on the track jumps to that fraction, arrows step by `step`.
  * date / time / month / week / datetime-local: the editor's segments come in the BROWSER's
    order (month/day/year, hour/minute/ampm on these builds, whatever the persona's locale), so the
    order is read from the editor itself, never assumed. Numeric segments take digits and move on
    after a full field; the year never moves on by itself (ArrowRight does); a month NAME field
    (type=month) ignores digits and steps on arrows; the ampm field takes A / P.
"""

import math
import random
import re

from ._motion import gauss_from, mulberry32

# ----------------------------------------------------------------------------- field classes
# The fill() routes. Playwright's own split (kInputTypesToSetValue / kInputTypesToTypeInto):
# text-like types are typed into, value types have their value set, everything else raises.
TEXT_TYPES = frozenset(["", "text", "search", "email", "password", "tel", "url", "number"])
DATETIME_TYPES = frozenset(["date", "time", "month", "week", "datetime-local"])
# color: Playwright sets it; the only humane route is the OS colour dialog, which no page sees and
# no script can drive -- so native, exactly as Playwright behaves.
NATIVE_VALUE_TYPES = frozenset(["color"])


def classify_field(tag, input_type=None, contenteditable=False):
    """'text' | 'range' | 'datetime' | 'native' | 'unfillable' for one element.

    'native' = let Playwright set the value (its semantics, untrusted but correct);
    'unfillable' = let Playwright's fill raise exactly as it would have (checkbox, file, ...)."""
    tag = (tag or "").lower()
    if contenteditable and tag not in ("input", "select"):
        return "text"
    if tag == "textarea":
        return "text"
    if tag != "input":
        return "unfillable"
    t = (input_type or "").lower()
    if t in TEXT_TYPES:
        return "text"
    if t == "range":
        return "range"
    if t in DATETIME_TYPES:
        return "datetime"
    if t in NATIVE_VALUE_TYPES:
        return "native"
    # An unknown type is a text input to the browser (HTML: invalid type -> text).
    if t not in ("checkbox", "radio", "file", "submit", "button", "image", "reset", "hidden"):
        return "text"
    return "unfillable"


# ----------------------------------------------------------------------------------- typos
# No fat-finger typos where a wrong character is not a harmless slip but a validation failure or a
# locked field: credentials, numbers, addresses, one-time codes.
NO_TYPO_TYPES = frozenset(["password", "email", "number", "tel", "url"])
NO_TYPO_INPUTMODES = frozenset(["numeric", "decimal", "tel", "email", "url"])
NO_TYPO_AUTOCOMPLETE = frozenset(["one-time-code", "cc-number", "cc-csc", "cc-exp", "current-password",
                                  "new-password"])

# Compact QWERTY adjacency for letter typos; digits slip to a NEIGHBOURING DIGIT on the number row
# (a digit never turns into a letter, and never into itself -- the old map returned the same digit
# for "typo" digits, which typed it, deleted it and typed it again).
NEARBY_LETTERS = {
    "a": "sqwz", "b": "vghn", "c": "xdfv", "d": "sfecx", "e": "wrsdf", "f": "dgrtcv",
    "g": "fhtyb", "h": "gjybn", "i": "ujko", "j": "hkunm", "k": "jloi", "l": "kop",
    "m": "njk", "n": "bhjm", "o": "iklp", "p": "ol", "q": "wa", "r": "edft",
    "s": "awedxz", "t": "rfgy", "u": "yhji", "v": "cfgb", "w": "qase", "x": "zsdc",
    "y": "tghu", "z": "asx",
}
NEARBY_DIGITS = {"1": "2", "2": "13", "3": "24", "4": "35", "5": "46", "6": "57", "7": "68",
                 "8": "79", "9": "80", "0": "9"}

DEFAULT_TYPO_RATE = 0.02


def typos_allowed(field):
    """May a fat-finger typo happen in this field? ``field`` = {type, inputmode, autocomplete} (any
    may be missing). Unknown fields allow typos, as the old behaviour did."""
    if not field:
        return True
    if field.get("frame"):
        return False   # focus is inside a frame we cannot read: fail closed
    t = (field.get("type") or "").lower()
    if t in NO_TYPO_TYPES:
        return False
    if (field.get("inputmode") or "").lower() in NO_TYPO_INPUTMODES:
        return False
    ac = (field.get("autocomplete") or "").lower().split()
    return not any(tok in NO_TYPO_AUTOCOMPLETE for tok in ac)


def typo_eligible(ch):
    """Characters a typo may replace: unshifted ASCII letters and digits."""
    return len(ch) == 1 and ("a" <= ch <= "z" or "0" <= ch <= "9")


def nearby_key(ch, rng=random):
    """A neighbouring key for ``ch`` (same class: letter -> letter, digit -> digit), or None."""
    if "a" <= ch <= "z":
        return rng.choice(NEARBY_LETTERS[ch])
    if "0" <= ch <= "9":
        return rng.choice(NEARBY_DIGITS[ch])
    return None


def plan_typo(text, i, rng=random, rate=DEFAULT_TYPO_RATE):
    """Decide whether character ``i`` of ``text`` gets a typo, and which kind.

    None, or a dict:
      {"kind": "sub", "wrong": c, "late": k}  -- a neighbouring key instead of text[i]; noticed
          after k (0-2) more correct characters, then Backspace k+1 times and retype.
      {"kind": "double"}                      -- text[i] pressed twice; one Backspace.
      {"kind": "swap"}                         -- text[i+1] typed before text[i]; two Backspaces.
    The humanize layer still checks that each wrong key actually landed in the field before it
    deletes anything (an input mask or maxlength can swallow it)."""
    ch = text[i]
    if not typo_eligible(ch) or rate <= 0 or rng.random() >= rate:
        return None
    r = rng.random()
    nxt = text[i + 1] if i + 1 < len(text) else None
    if r < 0.15 and nxt is not None and typo_eligible(nxt) and nxt != ch:
        return {"kind": "swap"}
    if r < 0.30:
        return {"kind": "double"}
    late = 0
    if rng.random() < 0.3:
        # Late notice: only over characters that are themselves plain unshifted keys, so the
        # retyping after the Backspaces stays on the simple path.
        k = 1 if rng.random() < 0.7 else 2
        tail = text[i + 1:i + 1 + k]
        if len(tail) == k and all(typo_eligible(c) or c == " " for c in tail):
            late = k
    return {"kind": "sub", "wrong": nearby_key(ch, rng), "late": late}


def formatted_equal(got, want):
    """Is ``got`` just ``want`` re-punctuated by an input mask -- the same letters and digits in the
    same order, with separators, spaces or case changed ("(555) 123-4567" for "5551234567")? A value
    only a formatter changed counts as typed right; a lost or an extra key does not. Typing it again
    would not change what the formatter does, and no person types their phone number twice."""
    if got is None or want is None:
        return False
    a = [c.casefold() for c in got if c.isalnum()]
    b = [c.casefold() for c in want if c.isalnum()]
    return bool(b) and a == b


# ------------------------------------------------------------------------------- maxlength
# The input types whose maxlength the browser enforces on what is typed or inserted (HTML: text,
# search, url, tel, email, password), plus textarea. number, date and the rest ignore it.
MAXLENGTH_TYPES = frozenset(["", "text", "search", "url", "tel", "email", "password"])


def fit_maxlength(value, maxlength, tag="input", input_type=""):
    """The part of ``value`` an EMPTY control with ``maxlength`` ends up holding: what Playwright's
    fill (Input.insertText) leaves there, and what a person types before the field stops taking keys.

    maxlength counts UTF-16 code units, as Blink does; a character that would be cut in half (an
    emoji beyond the BMP) is dropped whole, like Blink's LimitLength. A negative or missing maxlength,
    a contenteditable and a type that ignores maxlength (number, date, ...) keep the whole value.

    Measured on r38 before this existed: fill('#zip', '1234567') on maxlength=5 typed all seven
    digits, saw 12345, wiped the field, typed seven again and then handed the field to Playwright."""
    if value is None:
        return value
    try:
        n = int(maxlength) if maxlength is not None else -1
    except (TypeError, ValueError):
        n = -1
    tag = (tag or "").lower()
    if n < 0 or tag not in ("input", "textarea"):
        return value
    if tag == "input" and (input_type or "").lower() not in MAXLENGTH_TYPES:
        return value
    units = 0
    for k, ch in enumerate(value):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > n:
            return value[:k]
    return value


def mask_kept(got, want, maxlength=None, tag="input", input_type=""):
    """Is ``got`` what a formatting mask leaves of ``want`` once the field stops taking keys -- a value a
    person typing ``want`` ends up with too, so typing it again (or handing it to Playwright, whose
    insert the same mask cuts the same way) changes nothing? Its letters and digits are a shorter prefix
    of ``want``'s (formatted_equal covers the whole value) AND either

    - the field is full: it holds ``maxlength`` UTF-16 units (an expiry field MM/YY with maxlength 5
      takes "12/34" of "12345": the slash the mask adds uses the fifth place), or
    - the page ADDED punctuation that was not typed there: a mask that formats AND cuts ("1234 AB" of
      "1234abx", a postcode). Only an added separator counts. A field that just changes case ("abc12"
      of "ABC123") or strips separators ("555123456" of "555-123-4567") shows no mask that cuts, so a
      missing last letter or digit there is a lost key (round 3: both used to be accepted silently).

    A value cut short with no punctuation added and room left is a lost key, not a mask: False, and
    the caller types it again. Verifier B (SC 210): MM/YY and a 12-place phone mask were typed, wiped
    with Ctrl+A Backspace and typed again, then handed to Playwright, for the same visible result."""
    if not got or want is None:
        return False
    a = [c.casefold() for c in got if c.isalnum()]
    b = [c.casefold() for c in want if c.isalnum()]
    if not a or len(a) >= len(b) or b[:len(a)] != a:
        return False
    try:
        n = int(maxlength) if maxlength is not None else -1
    except (TypeError, ValueError):
        n = -1
    t = (tag or "").lower()
    if n >= 0 and (t == "textarea" or t == "input" and (input_type or "").lower() in MAXLENGTH_TYPES):
        if sum(2 if ord(ch) > 0xFFFF else 1 for ch in got) >= n:
            return True
    g, w = _sep_runs(got), _sep_runs(want)
    return any(c not in w[i] for i in range(len(g)) for c in g[i])


def _sep_runs(s):
    """The separators of ``s`` by place: run i is what stands before its i-th letter or digit, and the
    last run is what trails the last one ("12/3" -> ["", "", "/", ""])."""
    runs, cur = [], []
    for c in s:
        if c.isalnum():
            runs.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    runs.append("".join(cur))
    return runs


# ------------------------------------------------------------------------------ pre-press pause
# The pause between the pointer coming to rest and the button going down. A hand that has arrived
# confirms the target before it presses: 40-250 ms, right-skewed (most presses come quickly, a few
# after a look). Measured on r38 before this existed: the focus click before typing pressed ~4 ms
# after the last pointermove (Playwright's mouse.click moves again right before it presses), a drag
# grab ~21 ms; the targeted click's flat 40-130 ms was the only humane one.
PRE_PRESS_MIN_MS = 40.0
PRE_PRESS_MAX_MS = 250.0
# Salt for the per-identity centre: a separate mulberry32 stream keeps make_persona's draw order (and
# every existing seed's motor identity) unchanged. Same value in forms.ts and Forms.cs.
_PRE_PRESS_SALT = 0x5BD1E995


def pre_press_center(seed_int):
    """The identity's typical pre-press pause (ms), 70..135, stable per persona seed."""
    r = mulberry32((int(seed_int) ^ _PRE_PRESS_SALT) & 0xFFFFFFFF)()
    return 70.0 + (135.0 - 70.0) * r


def pre_press_ms(seed_int, rng=None):
    """One pre-press pause (ms): a log-normal above the 40 ms floor, its median at the identity's centre
    (sigma 0.55, so right-skewed and never below the floor). A draw past 250 ms is drawn again (up to
    four times, then capped), so no value piles up at either bound."""
    if rng is None:
        rng = random.random
    c = pre_press_center(seed_int) - PRE_PRESS_MIN_MS
    ms = PRE_PRESS_MAX_MS
    for _ in range(4):
        ms = PRE_PRESS_MIN_MS + c * math.exp(0.55 * gauss_from(rng))
        if ms <= PRE_PRESS_MAX_MS:
            return ms
    return PRE_PRESS_MAX_MS


# ------------------------------------------------------------------------------ select-all
def select_all_combo(platform):
    """The select-all chord a person on ``platform`` uses: Cmd+A on a Mac, Ctrl+A elsewhere."""
    return "Meta+a" if (platform or "").lower() in ("macos", "mac", "darwin") else "Control+a"


# ------------------------------------------------------------------------------ click options
_MODIFIERS = ("Alt", "Control", "ControlOrMeta", "Meta", "Shift")


def normalize_click_options(options, platform=None):
    """Playwright click/dblclick/hover options -> what the humanized press needs.

    The old wrappers forwarded only `timeout`, so `button="right"` became a left click, modifiers
    and `position` were dropped, and `trial=True` -- documented to perform NO action -- clicked.

    "ControlOrMeta" is kept as is: Playwright resolves it to the key the ENGINE's platform uses, which
    is what makes Ctrl+click open a link in a new tab on a Windows or Linux build whatever the
    persona claims. (``platform`` is accepted for API symmetry and unused.)"""
    o = options or {}
    button = o.get("button") or "left"
    if button not in ("left", "right", "middle"):
        button = "left"
    count = o.get("click_count", o.get("clickCount"))
    count = int(count) if count else 1
    mods = []
    for m in o.get("modifiers") or []:
        if m in _MODIFIERS and m not in mods:
            mods.append(m)
    pos = o.get("position")
    position = {"x": float(pos["x"]), "y": float(pos["y"])} if isinstance(pos, dict) and "x" in pos and "y" in pos else None
    delay = o.get("delay")
    try:
        delay = float(delay) if delay is not None else None
    except (TypeError, ValueError):
        delay = None
    # delay=0 is Playwright's DEFAULT ("no hold"), not a request for a 1 ms press: it, and anything
    # non-positive or non-finite, means "use the persona's hold" (a 1-4 ms button hold is a tell, and a
    # negative hold would leave the button down).
    if delay is not None and (not math.isfinite(delay) or delay <= 0):
        delay = None
    return {
        "button": button, "click_count": max(1, count), "modifiers": mods,
        "delay": delay, "position": position,
        "force": bool(o.get("force")), "trial": bool(o.get("trial")),
    }


def engine_path_ok(opts):
    """The engine pointer path performs one plain left click and nothing else."""
    return (opts["button"] == "left" and opts["click_count"] == 1 and not opts["modifiers"]
            and opts["delay"] is None)


# r43 engine (SC 232 lane H): Browser.dispatchPointerPath also takes fromX/fromY, button, clickCount, modifiers,
# buttons (held) and settle, and answers after the last event it sends; Browser.dispatchKeySequence types a run paced
# in the browser. Both are detected together (see _humanize._engine_ext_ok).
ENGINE_KEY_SEQUENCE = "Browser.dispatchKeySequence"
_CDP_MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
_BUTTON_BITS = {"left": 1, "right": 2, "middle": 4}


def engine_click_params(opts):
    """The extra dispatchPointerPath parameters for a click an r43 engine can perform itself ({} for a plain left
    click), or None when it cannot: a caller-chosen hold (`delay`), an unknown button, more than three clicks.
    Modifier KEYS are still pressed by the SDK around the call (the engine only sets the event flags)."""
    if opts["delay"] is not None or opts["button"] not in _BUTTON_BITS or not 1 <= opts["click_count"] <= 3:
        return None
    p = {}
    if opts["button"] != "left":
        p["button"] = opts["button"]
    if opts["click_count"] != 1:
        p["clickCount"] = int(opts["click_count"])
    bits = 0
    for m in opts["modifiers"]:
        bits |= _CDP_MODIFIER_BITS.get(m, 0)
    if bits:
        p["modifiers"] = bits
    return p


def engine_held_bits(button):
    """dispatchPointerPath `buttons` for a held button name."""
    return _BUTTON_BITS.get(button or "left", 1)


# US-layout DOM codes for the punctuation keys (letters, digits and space need none: the engine knows them).
_US_CODES = {",": "Comma", ".": "Period", "/": "Slash", ";": "Semicolon", "'": "Quote", "[": "BracketLeft",
             "]": "BracketRight", "-": "Minus", "=": "Equal", "`": "Backquote", "\\": "Backslash",
             "<": "Comma", ">": "Period", "?": "Slash", ":": "Semicolon", '"': "Quote", "{": "BracketLeft",
             "}": "BracketRight", "_": "Minus", "+": "Equal", "~": "Backquote", "|": "Backslash",
             "!": "Digit1", "@": "Digit2", "#": "Digit3", "$": "Digit4", "%": "Digit5", "^": "Digit6",
             "&": "Digit7", "*": "Digit8", "(": "Digit9", ")": "Digit0"}


def engine_key_plan(text, dwell_ms, gap_ms, needs_shift):
    """Browser.dispatchKeySequence keys for `text` on a US layout: dwell_ms() per key, gap_ms(ch, nxt) after each
    key but the last (the caller's cadence model: lane D's bigram flight), Shift (modifiers 8) for the characters
    needs_shift(ch) says. None when a character has no plain US key (the caller types it the old way)."""
    keys = []
    for i, ch in enumerate(text):
        if ch == "\n":
            k = {"key": "Enter", "code": "Enter"}
        elif ch == "\t":
            k = {"key": "Tab", "code": "Tab"}
        elif ch.isascii() and (ch.isalnum() or ch == " "):
            k = {"key": ch}
        elif ch in _US_CODES:
            k = {"key": ch, "code": _US_CODES[ch]}
        else:
            return None
        k["dwellMs"] = float(dwell_ms())
        k["gapMs"] = float(gap_ms(ch, text[i + 1])) if i < len(text) - 1 else 0.0
        if needs_shift(ch):
            k["modifiers"] = 8
        keys.append(k)
    return keys


# ------------------------------------------------------------------- <select> type-ahead plan
def _fold(s):
    """Blink compares FoldCase()d labels with leading whitespace stripped (type_ahead.cc)."""
    return (s or "").lstrip().casefold()


# A space counts: inside a running type-ahead search Blink appends it like any other character, so
# "item 371" lands on "Item 371" in a 400-row list (measured on r37 by verifier B: 1 trusted change,
# 0.99 s). Without it the prefix stopped at "item" and the other 370 rows were ArrowDowns (68 s). It
# is never the FIRST key: labels are compared with leading whitespace stripped, and a space that
# starts a search can open or close the popup instead.
TYPEABLE = frozenset("abcdefghijklmnopqrstuvwxyz0123456789 ")


def typeahead_simulate(labels, start, keys):
    """Where Blink's TypeAhead (kMatchPrefix | kCycleFirstChar) lands after ``keys``.

    ``labels``: one entry per option, None for a disabled option (Blink's OptionAtIndex returns an
    empty string for those, so they never match). ``start``: the highlighted index when typing
    begins (the current selection; -1 = none). Returns the landing index (unchanged if no match).

    Mirrors type_ahead.cc: a first key, or the same key again, searches for that ONE character
    starting AFTER the current item (so a repeated letter cycles through the items that start with
    it); a mixed buffer searches for the whole buffer starting AT the current item."""
    n = len(labels)
    if n == 0:
        return start
    folded = [_fold(l) if l is not None else None for l in labels]
    buf = ""
    repeating = None
    cur = start
    for c in keys:
        c = c.casefold()
        buf += c
        offset = 1
        if c == repeating:
            prefix = c
        else:
            prefix = buf
            if len(buf) > 1:
                repeating = None
                offset = 0
            else:
                repeating = c
        idx = ((cur if cur >= 0 else 0) + offset) % n
        for _ in range(n):
            lab = folded[idx]
            if lab is not None and lab != "" and lab.startswith(prefix):
                cur = idx
                break
            idx = (idx + 1) % n
    return cur


def _enabled_steps(labels, frm, to):
    """Arrow presses from row ``frm`` to row ``to`` in the popup, which skips disabled rows (signed).
    Each press lands on the next enabled row, so the count is the enabled rows passed through INCLUDING
    the target: (frm, to] going down, [to, frm) going up -- the start row itself never counts, which is
    what makes a move away from a disabled current row come out right."""
    if frm == to:
        return 0
    if labels[to] is None:
        return None
    if to > frm:
        return sum(1 for i in range(frm + 1, to + 1) if labels[i] is not None)
    return -sum(1 for i in range(to, frm) if labels[i] is not None)


def plan_select(labels, current, target, max_prefix=24):
    """Keys that move an OPEN drop-down's highlight from ``current`` to ``target``.

    Returns {"keys": "<letters to type>", "arrows": <signed ArrowDown count>} with the fewest
    presses: the shortest typeable prefix of the target's label that type-ahead lands on exactly,
    else the cheapest (prefix, then arrows) combination, else arrows alone. None when the target
    is disabled (no route selects it)."""
    if not (0 <= target < len(labels)) or labels[target] is None:
        return None
    best = None
    start = current if 0 <= current < len(labels) else -1
    base = _enabled_steps(labels, start if start >= 0 else 0, target) if start >= 0 else None
    if base is not None:
        best = {"keys": "", "arrows": base}
    elif start < 0:
        # No selection: the popup highlights the first row; arrows count from there.
        first = next((i for i, l in enumerate(labels) if l is not None), None)
        if first is not None:
            st = _enabled_steps(labels, first, target)
            if st is not None:
                best = {"keys": "", "arrows": st}
    label = _fold(labels[target])
    prefix = ""
    for ch in label[:max_prefix]:
        if ch not in TYPEABLE or (ch == " " and not prefix):
            break
        prefix += ch
        land = typeahead_simulate(labels, start, prefix)
        if land < 0 or labels[land] is None:
            continue
        st = _enabled_steps(labels, land, target)
        if st is None:
            continue
        cost = len(prefix) + abs(st)
        best_cost = None if best is None else len(best["keys"]) + abs(best["arrows"])
        # fewest presses; on a tie, typing the name outright beats typing and then arrowing
        if best is None or cost < best_cost or (cost == best_cost and st == 0 and best["arrows"] != 0):
            best = {"keys": prefix, "arrows": st}
        if st == 0:
            break
    return best


def match_option(options, value=None, index=None, label=None):
    """Playwright's option matching for ONE wanted option: a string `value` matches an option's
    value OR its label; `label` matches the label; `index` the position. ``options`` = list of
    {"value", "label", "disabled"}. Returns the index or -1."""
    if index is not None:
        return index if 0 <= index < len(options) else -1
    for i, o in enumerate(options):
        if label is not None:
            if o.get("label") == label:
                return i
        elif value is not None and (o.get("value") == value or o.get("label") == value):
            return i
    return -1


# --------------------------------------------------------------------------------- range
def _num(v, default):
    try:
        f = float(v)
        return f if f == f else default   # NaN -> default
    except (TypeError, ValueError):
        return default


def range_attrs(min_=None, max_=None, step=None):
    """HTML defaults: min 0, max 100, step 1 ('any' = no snapping); max below min -> max = min."""
    lo = _num(min_, 0.0)
    hi = _num(max_, 100.0)
    if hi < lo:
        hi = lo
    if isinstance(step, str) and step.strip().lower() == "any":
        st = None
    else:
        st = _num(step, 1.0)
        if st <= 0:
            st = 1.0
    return lo, hi, st


def range_sanitize(value, min_=None, max_=None, step=None):
    """The value the browser will hold after being asked for ``value`` (clamped, step-aligned from
    min, rounded to the nearest step; a tie goes up, as Blink's StepRange does)."""
    lo, hi, st = range_attrs(min_, max_, step)
    v = min(max(_num(value, (lo + hi) / 2), lo), hi)
    if st:
        # Blink steps in Decimal; the epsilon keeps binary floats from rounding 0.35/0.1 =
        # 3.4999999999999996 down to 3 where the browser gets 4. The step count is capped at the
        # last step inside max -- computed, never walked down in a loop: a page can serve
        # max=123456789 step=1e-9, where subtracting one step leaves the float unchanged forever.
        q, last = (v - lo) / st, (hi - lo) / st
        if not (math.isfinite(q) and math.isfinite(last)):
            return v    # a step too small to count in floats: nothing to snap to
        n = min(math.floor(q + 0.5 + 1e-9), math.floor(last + 1e-9))
        v = lo + n * st
    return v


def range_value_ok(value, min_=None, max_=None, step=None):
    """Would the browser keep ``value`` exactly? Playwright's fill sets the value and raises
    "Malformed value" when the browser changed it (off the step, out of range, not a number), so the
    humane route only takes values that pass and leaves the rest to Playwright to reject."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(f):
        return False
    return numeric_equal(range_sanitize(f, min_, max_, step), f)


def range_fraction(value, min_=None, max_=None):
    lo, hi, _ = range_attrs(min_, max_, None)
    return 0.0 if hi == lo else (value - lo) / (hi - lo)


# Chromium's default slider thumb is ~16 px wide; the track's usable span is inset by half of it.
RANGE_THUMB_INSET = 8.0


def range_point(box, fraction, rtl=False, inset=RANGE_THUMB_INSET):
    """Where on a horizontal range input the thumb sits at ``fraction`` (CSS px)."""
    w = box["width"]
    ins = min(inset, w / 2.0)
    f = 1.0 - fraction if rtl else fraction
    return box["x"] + ins + f * (w - 2 * ins), box["y"] + box["height"] / 2.0


def range_nudge(current, target, step):
    """Arrow presses from ``current`` to ``target``: ('ArrowRight'|'ArrowLeft', count). ArrowRight
    raises a horizontal LTR range by one step (and lowers an RTL one; the caller swaps)."""
    if step is None or step <= 0 or abs(target - current) < 1e-9:
        return None, 0
    n = int(round(abs(target - current) / step))
    return ("ArrowRight" if target > current else "ArrowLeft"), n


def numeric_equal(a, b):
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False


# ----------------------------------------------------------------- date / time / month / week
def parse_datetime_value(kind, value):
    """Parse an HTML date-like value into its parts, or None if it is not valid for ``kind``."""
    v = (value or "").strip()
    try:
        if kind == "date":
            m = re.fullmatch(r"(\d{4,6})-(\d{2})-(\d{2})", v)
            if not m:
                return None
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return {"year": y, "month": mo, "day": d} if 1 <= mo <= 12 and 1 <= d <= 31 else None
        if kind == "month":
            m = re.fullmatch(r"(\d{4,6})-(\d{2})", v)
            if not m:
                return None
            y, mo = int(m.group(1)), int(m.group(2))
            return {"year": y, "month": mo} if 1 <= mo <= 12 else None
        if kind == "week":
            m = re.fullmatch(r"(\d{4,6})-W(\d{2})", v)
            if not m:
                return None
            y, w = int(m.group(1)), int(m.group(2))
            return {"year": y, "week": w} if 1 <= w <= 53 else None
        if kind == "time":
            return _parse_time(v)
        if kind == "datetime-local":
            m = re.fullmatch(r"(\d{4,6})-(\d{2})-(\d{2})[T ](.+)", v)
            if not m:
                return None
            t = _parse_time(m.group(4))
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if t is None or not (1 <= mo <= 12 and 1 <= d <= 31):
                return None
            t.update({"year": y, "month": mo, "day": d})
            return t
    except (TypeError, ValueError):
        return None
    return None


def _parse_time(v):
    m = re.fullmatch(r"(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?", v)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    s = int(m.group(3)) if m.group(3) else 0
    ms = int((m.group(4) or "0").ljust(3, "0")) if m.group(4) else 0
    if h > 23 or mi > 59 or s > 59:
        return None
    return {"hour": h, "minute": mi, "second": s, "millisecond": ms}


def datetime_equal(kind, a, b):
    """Same moment for ``kind``, whatever the formatting (17:45 == 17:45:00)."""
    pa, pb = parse_datetime_value(kind, a), parse_datetime_value(kind, b)
    return pa is not None and pa == pb


def plan_datetime(kind, segments, value, current=None):
    """Key actions that type ``value`` into a native date-like editor whose fields come in
    ``segments`` order (names as the editor's pseudo elements give them, '-field' optional:
    month, day, year, hour, minute, second, millisecond, ampm, week).

    Returns a list of actions: ("type", "digits") | ("key", "ArrowRight") | ("key", "ArrowUp"/
    "ArrowDown", n) -- or None when the editor cannot represent the value (a seconds value with no
    seconds field, a year beyond four digits, an unknown segment)."""
    parts = parse_datetime_value(kind, value)
    if parts is None:
        return None
    segs = [s.replace("-field", "") for s in segments]
    if not segs:
        return None
    have_ampm = "ampm" in segs
    if kind in ("time", "datetime-local"):
        if parts.get("second") and "second" not in segs:
            return None
        if parts.get("millisecond") and "millisecond" not in segs:
            return None
    cur = parse_datetime_value(kind, current) if current else None
    out = []
    for k, seg in enumerate(segs):
        last = k == len(segs) - 1
        if seg == "year":
            y = parts.get("year")
            if y is None or y > 9999:
                return None
            out.append(("type", "%04d" % y))
            if not last:
                out.append(("key", "ArrowRight"))      # the year never moves on by itself
        elif seg == "month":
            m = parts.get("month")
            if m is None:
                return None
            if kind == "month":
                # A month NAME field: digits are ignored; it steps on arrows. From empty the first
                # ArrowUp shows January.
                c = cur.get("month") if cur else None
                if c is None:
                    out.append(("key", "ArrowUp", m))
                else:
                    d = (m - c) % 12
                    out.append(("key", "ArrowUp", d) if d <= 6 else ("key", "ArrowDown", 12 - d))
                if not last:
                    out.append(("key", "ArrowRight"))
            else:
                out.append(("type", "%02d" % m))
        elif seg == "day":
            d = parts.get("day")
            if d is None:
                return None
            out.append(("type", "%02d" % d))
        elif seg == "week":
            w = parts.get("week")
            if w is None:
                return None
            out.append(("type", "%02d" % w))
        elif seg == "hour":
            h = parts.get("hour")
            if h is None:
                return None
            if have_ampm:
                h12 = h % 12 or 12
                out.append(("type", "%02d" % h12))
            else:
                out.append(("type", "%02d" % h))
        elif seg == "minute":
            out.append(("type", "%02d" % parts.get("minute", 0)))
        elif seg == "second":
            out.append(("type", "%02d" % parts.get("second", 0)))
        elif seg == "millisecond":
            out.append(("type", "%03d" % parts.get("millisecond", 0)))
        elif seg == "ampm":
            # lower case: a capital with no Shift held is input no keyboard produces
            out.append(("type", "a" if parts.get("hour", 0) < 12 else "p"))
        else:
            return None
    # drop no-op arrow steps (a month already right)
    return [a for a in out if not (a[0] == "key" and len(a) == 3 and a[2] == 0)]


def segment_names_from_tree(node):
    """The date-like editor's field names, in order, from a CDP DOM.describeNode(pierce) tree."""
    out = []

    def walk(n):
        attrs = n.get("attributes") or []
        for j in range(0, len(attrs) - 1, 2):
            if attrs[j] == "pseudo":
                ps = attrs[j + 1]
                if ps.startswith("-webkit-datetime-edit-") and (ps.endswith("-field")):
                    out.append(ps[len("-webkit-datetime-edit-"):])
        for c in (n.get("shadowRoots") or []) + (n.get("children") or []):
            walk(c)

    walk(node)
    return out


def segment_nodes_from_tree(node):
    """Like segment_names_from_tree but with each field's backendNodeId: [(name, backendNodeId)]."""
    out = []

    def walk(n):
        attrs = n.get("attributes") or []
        for j in range(0, len(attrs) - 1, 2):
            if attrs[j] == "pseudo":
                ps = attrs[j + 1]
                if ps.startswith("-webkit-datetime-edit-") and ps.endswith("-field"):
                    out.append((ps[len("-webkit-datetime-edit-"):], n.get("backendNodeId")))
        for c in (n.get("shadowRoots") or []) + (n.get("children") or []):
            walk(c)

    walk(node)
    return out
