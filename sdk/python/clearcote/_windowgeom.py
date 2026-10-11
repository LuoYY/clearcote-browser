"""The persona browser window: maximized, snapped, or a restored rectangle, derived from a seed and
the final screen / work area.

This is a bit-exact port of the engine's ``components/ungoogled/persona_window_geometry.cc``
(r39). The engine applies it natively to every persona launch (``--fingerprint=<seed>`` on r39+):
the headless display becomes the persona's display and the first browser window is created with
the persona's bounds and show state, so ``outerWidth``/``outerHeight``/``screenX``/``screenY`` are
the real window's. The SDK uses this port only where no persona runs in the engine (regime 2: a
seedless launch, ``light_stealth``), so those launches get the same distribution instead of always
maximizing, and in its tests (the PARITY vector is checked against the engine's own output).

Integer arithmetic only, with every 64-bit operation masked, so Python, Node (BigInt) and .NET
(ulong) agree on every rectangle.
"""

MASK64 = (1 << 64) - 1

ALGORITHM_VERSION = 1
MIN_AVAIL_WIDTH = 800
MIN_AVAIL_HEIGHT = 560

_WINDOW_SALT = 0x5B1E7A3C9D24F681
_WINDOW_KEY_SALT = 0xC2B2AE3D27D4EB4F
_MIN_WIDTH = 900
_MIN_HEIGHT = 600
_MIN_SNAP_WIDTH = 900
_MAX_TOP_OFFSET = 65
_MAX_NEAR_OFFSET = 60

PLATFORMS = {"windows": 0, "linux": 1, "macos": 2}

# (maximized, near, snap, restored) per-mille by work-area width: <= 1440, <= 1680, <= 1920,
# <= 2560, wider. Each row sums to 1000. Same table as the engine.
_WEIGHTS = (
    (700, 50, 0, 250),
    (670, 50, 0, 280),
    (640, 50, 10, 300),
    (530, 50, 10, 410),
    (450, 50, 10, 490),
)


def _mix(z):
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    return z ^ (z >> 31)


class _Stream:
    """SplitMix64, as in the engine."""

    def __init__(self, state):
        self.state = state & MASK64

    def next(self):
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        return _mix(self.state)

    def below(self, n):
        return self.next() % n

    def range(self, lo, hi):
        return lo + self.below(hi - lo + 1)


def _u32(v):
    return v & 0xFFFFFFFF


def _input_hash(seed, sw, sh, aw, ah, platform, salt):
    h = _mix((seed & MASK64) ^ salt)
    h = _mix(h ^ ((_u32(sw) << 32) | _u32(sh)))
    h = _mix(h ^ ((_u32(aw) << 32) | _u32(ah)))
    h = _mix(h ^ ((platform << 8) | ALGORITHM_VERSION))
    return h


def _permille(total, pm):
    return total * pm // 1000


def _skewed(s):
    return min(s.range(0, 1000), s.range(0, 1000))


def _span(s, total, lo_pm, hi_pm, floor):
    lo = min(total, max(floor, _permille(total, lo_pm)))
    hi = min(total, max(lo, _permille(total, hi_pm)))
    return s.range(lo, hi)


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _draw_restored(s, aw, ah, g):
    min_w = min(aw, _MIN_WIDTH)
    min_h = min(ah, _MIN_HEIGHT)
    w = _span(s, aw, 420, 900, min_w)
    h = _span(s, ah, 780, 1000, min_h) if s.below(10) < 8 else _span(s, ah, 450, 780, min_h)
    lo_w = max(min_w, (11 * h + 9) // 10)
    hi_w = min(aw, 2 * h)
    if lo_w <= hi_w:
        w = _clamp(w, lo_w, hi_w)
    else:
        h = _clamp(h, max(min_h, (w + 1) // 2), min(ah, max(min_h, 10 * w // 11)))
    g["width"], g["height"] = w, h
    slack_x, slack_y = aw - w, ah - h
    g["y"] = _permille(min(slack_y, _MAX_TOP_OFFSET), _skewed(s))
    where = s.below(100)
    if where < 25:
        g["x"] = 0
    elif where < 35:
        g["x"] = _permille(slack_x, s.range(1, 100))
    else:
        g["x"] = _permille(slack_x, (s.range(0, 1000) + s.range(0, 1000)) // 2)


def _platform_code(platform):
    if isinstance(platform, int):
        return platform
    return PLATFORMS.get(str(platform or "windows").lower(), 0)


def derive_window(seed, screen_width, screen_height, avail_width, avail_height, platform="windows"):
    """The window for an identity seed (uint64) on a work area, as a dict
    ``{"valid", "state", "kind", "x", "y", "width", "height"}`` (``state`` is "maximized" or
    "normal"; x/y are relative to the work area's top-left), or ``{"valid": False}`` when the
    work area is too small to be a desktop's."""
    sw, sh, aw, ah = int(screen_width), int(screen_height), int(avail_width), int(avail_height)
    if aw < MIN_AVAIL_WIDTH or ah < MIN_AVAIL_HEIGHT or sw < aw or sh < ah:
        return {"valid": False}
    plat = _platform_code(platform)
    s = _Stream(_input_hash(seed, sw, sh, aw, ah, plat, _WINDOW_SALT))
    mx, near, snap_w, _restored = _WEIGHTS[0 if aw <= 1440 else 1 if aw <= 1680 else
                                           2 if aw <= 1920 else 3 if aw <= 2560 else 4]
    snap = snap_w if (plat == PLATFORMS["windows"] and aw // 2 >= _MIN_SNAP_WIDTH) else 0
    pick = s.below(1000)
    g = {"valid": True, "x": 0, "y": 0, "width": 0, "height": 0}
    pick -= mx
    if pick < 0:
        g["state"], g["kind"] = "maximized", "maximized"
        _draw_restored(s, aw, ah, g)
        return g
    g["state"] = "normal"
    pick -= near
    if pick < 0:
        g["kind"] = "near"
        g["width"] = _span(s, aw, 900, 990, min(aw, _MIN_WIDTH))
        g["height"] = _span(s, ah, 900, 990, min(ah, _MIN_HEIGHT))
        g["x"] = _permille(min(aw - g["width"], _MAX_NEAR_OFFSET), s.range(0, 1000))
        g["y"] = _permille(min(ah - g["height"], _MAX_NEAR_OFFSET), s.range(0, 1000))
        return g
    pick -= snap
    if pick < 0:
        right = s.below(5) < 2
        g["kind"] = "snap-right" if right else "snap-left"
        g["width"], g["height"] = aw // 2, ah
        g["x"], g["y"] = (aw - aw // 2 if right else 0), 0
        return g
    g["kind"] = "restored"
    _draw_restored(s, aw, ah, g)
    return g


def window_key(seed, screen_width, screen_height, avail_width, avail_height, platform="windows"):
    """The engine's saved-placement tag value (``PersonaWindowKey``) for these inputs."""
    return _input_hash(seed, int(screen_width), int(screen_height), int(avail_width),
                       int(avail_height), _platform_code(platform), _WINDOW_KEY_SALT)


def fnv1a64(text):
    """FNV-1a 64 of a UTF-8 string: the engine's PersonaSeedFromString for non-numeric values."""
    h = 0xCBF29CE484222325
    for b in str(text).encode("utf-8"):
        h ^= b
        h = (h * 0x100000001B3) & MASK64
    return h


def mixed_seed(raw_seed):
    """The engine's persona_seed for a ``--fingerprint`` value: the value itself when it parses as
    an unsigned 64-bit decimal, else FNV-1a 64, then one SplitMix64 step (MixedPersonaSeed)."""
    s = str(raw_seed)
    seed = int(s) & MASK64 if s.isdigit() and int(s) <= MASK64 else fnv1a64(s)
    z = (seed + 0x9E3779B97F4A7C15) & MASK64
    return _mix(z)
