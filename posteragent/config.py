"""Tunables for the poster agent demo.

Kept in one place because several of these numbers are load-bearing and the
PosterForest paper does not specify them (see docs/posterforest-notes.md).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# --- models -----------------------------------------------------------------
# claude-opus-5 is reachable only over the Anthropic Messages API on this
# proxy; the OpenAI-compatible route answers 400 "does not support the chat
# interface". llm.py dispatches on the name, so these can be mixed freely
# (e.g. a Claude text model with a qwen vision model).
TEXT_MODEL = os.environ.get("POSTER_TEXT_MODEL", "claude-opus-5")
VISION_MODEL = os.environ.get("POSTER_VISION_MODEL", "claude-opus-5")

# --- time budget ------------------------------------------------------------
# Wall-clock cap on one generation run. When it is reached the pipeline stops
# refining and returns the best poster it has rather than failing, so the
# artifact is always usable.
TOTAL_BUDGET_S = float(os.environ.get("POSTER_BUDGET_S", 20 * 60))
REQUEST_TIMEOUT_S = float(os.environ.get("POSTER_TIMEOUT", 300))

# --- poster canvas ----------------------------------------------------------
# PosterForest standardises on 48x36in (06_appendix.tex:262).
POSTER_SIZES = {
    "48x36 in (PosterForest standard)": (48.0, 36.0),
    "36x24 in": (36.0, 24.0),
    "A0 landscape (46.8x33.1 in)": (46.8, 33.1),
    "A0 portrait (33.1x46.8 in)": (33.1, 46.8),
}
DEFAULT_SIZE = "48x36 in (PosterForest standard)"

PREVIEW_PX_PER_IN = 46  # 48in -> 2208px wide; VLM sees a downscale of this
VLM_MAX_PX = 1280  # longest edge sent to the vision model

# --- split-tree geometry ----------------------------------------------------
# delta(0-5) -> ratio change. Paper gives no arithmetic; main_2.pdf shows
# observed steps of 0.1 and 0.2, so delta=5 -> 0.20.
RATIO_STEP_MAX = 0.20
RATIO_MIN, RATIO_MAX = 0.25, 0.75  # hard clamp, NOT left to the prompt
# Column widths get a tighter clamp: a column narrower than ~22% of its band
# cannot hold readable body text.
COLUMN_RATIO_MIN, COLUMN_RATIO_MAX = 0.22, 0.78

# --- content volume control -------------------------------------------------
# PosterForest accepts 80-100%; we raise the floor to 90 because the paper
# names "suboptimal content density" as a limitation (05_conclusion.tex:21).
VOLUME_TARGET_LO = 90.0
VOLUME_TARGET_HI = 100.0
# Below this fill a text panel is reported (non-blocking) as underfilled.
UNDERFILL_PCT = 60.0

# --- refinement loop --------------------------------------------------------
# Two rounds = two content and two layout passes per schedule (proposal s5.1);
# the order lives in pipeline.ORDERS.

# --- dialogue revision (our RQ2, not in PosterForest) -----------------------
MAX_DIALOGUE_TURNS = 3
MAX_EDIT_ATTEMPTS = 2

# --- per-pass model-call caps ----------------------------------------------
# Identical for every schedule, so joint / content-first / layout-first get
# the same number of feedback opportunities and the same call ceiling.
MAX_CONTENT_CALLS = 8  # text panels edited per content pass
MAX_LAYOUT_CALLS = 6  # vision calls per layout pass

# --- space allocation -------------------------------------------------------
# Ratio steps the measured allocator tries when a panel asks for more room.
ALLOC_STEPS = (0.03, 0.06, 0.10, 0.15)

# --- layout init weights ----------------------------------------------------
# Calibrated against the benchmark's ~774 words / 8.7 figures per poster.
CHAR_COST = 1.0
FIG_COST = 900.0
TAB_COST = 700.0
N_COLUMNS = 3

# --- typography (points, at 48in wide; scaled for other sizes) --------------
PT_TITLE = 78
PT_AUTHORS = 34
PT_SECTION = 40
PT_BODY = 27
PT_CAPTION = 19
MIN_FONT_SCALE = 0.72  # below this we flag overflow instead of shrinking more

def _font_path(env: str, candidates: tuple[str, ...]) -> str:
    """Measurement font. DejaVu Sans is the reference (CI installs it); its
    glyphs are wider than Arial's, so text that measures as fitting also fits
    in the exported PPTX. Override with POSTER_FONT / POSTER_FONT_BOLD."""
    if os.environ.get(env):
        return os.environ[env]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise RuntimeError(f"No TrueType font found; install DejaVu Sans or set {env}")


FONT_REGULAR = _font_path("POSTER_FONT", (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Verdana.ttf",
    "C:/Windows/Fonts/DejaVuSans.ttf",
    "C:/Windows/Fonts/verdana.ttf",
))
FONT_BOLD = _font_path("POSTER_FONT_BOLD", (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
    "C:/Windows/Fonts/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/verdanab.ttf",
))

# --- palette ----------------------------------------------------------------
COL_BG = (255, 255, 255)
COL_TITLE_BG = (31, 59, 95)
COL_TITLE_FG = (255, 255, 255)
COL_PANEL_BG = (247, 249, 251)
COL_HEADER_BG = (70, 110, 155)
COL_HEADER_FG = (255, 255, 255)
COL_TEXT = (28, 32, 38)
COL_CAPTION = (90, 98, 110)
COL_BORDER = (205, 214, 224)
COL_OVERFLOW = (200, 60, 60)


# --- thresholds frozen by the evaluation plan ------------------------------
# The proposal fixes "dimensions and readability thresholds ... on the
# development set ... before testing". These are those thresholds plus the
# per-pass budgets every schedule shares. evaluate.py writes them into
# plan.json, applies the plan's values before a run and records them.
THRESHOLDS: dict[str, type] = {
    "MIN_FONT_SCALE": float,
    "PT_TITLE": float, "PT_AUTHORS": float, "PT_SECTION": float, "PT_BODY": float, "PT_CAPTION": float,
    "VOLUME_TARGET_LO": float, "VOLUME_TARGET_HI": float, "UNDERFILL_PCT": float,
    "RATIO_MIN": float, "RATIO_MAX": float, "COLUMN_RATIO_MIN": float, "COLUMN_RATIO_MAX": float,
    "MAX_CONTENT_CALLS": int, "MAX_LAYOUT_CALLS": int, "MAX_EDIT_ATTEMPTS": int,
}


def thresholds() -> dict:
    """Current values, keyed as in plan.json (lower case)."""
    return {k.lower(): globals()[k] for k in THRESHOLDS}


def apply_thresholds(values: dict) -> dict:
    """Set every threshold from `values`; returns what was applied.

    All keys must be present and known, and the values coherent: a plan that
    silently fell back to a code default would not be frozen at all.
    """
    want = {k.lower() for k in THRESHOLDS}
    unknown, missing = set(values) - want, want - set(values)
    if unknown or missing:
        raise ValueError("plan thresholds: " + "; ".join(
            ([f"unknown {sorted(unknown)}"] if unknown else []) + ([f"missing {sorted(missing)}"] if missing else [])))
    new = {}
    for k, typ in THRESHOLDS.items():
        v = values[k.lower()]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (typ is int and int(v) != v):
            raise ValueError(f"plan thresholds: {k.lower()} must be {'an integer' if typ is int else 'a number'}")
        new[k] = typ(v)
    checks = [
        (0 < new["MIN_FONT_SCALE"] <= 1, "min_font_scale must be in (0, 1]"),
        (all(new[k] > 0 for k in ("PT_TITLE", "PT_AUTHORS", "PT_SECTION", "PT_BODY", "PT_CAPTION")),
         "font sizes must be positive"),
        (0 < new["UNDERFILL_PCT"] <= new["VOLUME_TARGET_LO"] < new["VOLUME_TARGET_HI"],
         "need 0 < underfill_pct <= volume_target_lo < volume_target_hi"),
        (0 < new["RATIO_MIN"] < 0.5 < new["RATIO_MAX"] < 1, "need 0 < ratio_min < 0.5 < ratio_max < 1"),
        (0 < new["COLUMN_RATIO_MIN"] < 0.5 < new["COLUMN_RATIO_MAX"] < 1,
         "need 0 < column_ratio_min < 0.5 < column_ratio_max < 1"),
        (all(new[k] >= 1 for k in ("MAX_CONTENT_CALLS", "MAX_LAYOUT_CALLS", "MAX_EDIT_ATTEMPTS")),
         "call caps and attempts must be >= 1"),
    ]
    bad = [msg for ok, msg in checks if not ok]
    if bad:
        raise ValueError("plan thresholds: " + "; ".join(bad))
    globals().update(new)
    return thresholds()


@dataclass(frozen=True)
class PosterSize:
    w_in: float
    h_in: float
    # Typography is tied to the *standard* canvas, not to this one. The
    # proposal shrinks the canvas to 75% area while keeping font sizes fixed,
    # so the smaller canvas is a controlled increase in space pressure. If
    # fonts scaled with the canvas there would be no pressure at all.
    type_scale: float | None = None

    @property
    def scale(self) -> float:
        """Font and padding scale relative to the 48in reference width."""
        return self.type_scale if self.type_scale is not None else self.w_in / 48.0

    def shrunk(self, area: float) -> "PosterSize":
        """Same aspect ratio, `area` times the area, typography unchanged."""
        k = area ** 0.5
        return PosterSize(self.w_in * k, self.h_in * k, self.scale)

    @property
    def px_w(self) -> int:
        return int(round(self.w_in * PREVIEW_PX_PER_IN))

    @property
    def px_h(self) -> int:
        return int(round(self.h_in * PREVIEW_PX_PER_IN))
