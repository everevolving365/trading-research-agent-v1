"""Reading marked-up screenshots of past trades (ability 15).

The client uploads chart screenshots with their own arrows, lines and notes on
them, and the agent infers the rules. This is the second intake path, offered
alongside the spoken one; both produce the same strategy spec and inherit every
guarantee in the system.

What it does and does not claim
-------------------------------
A vision model reads what is *drawn* on the chart and returns it as structured
observations -- a level here, an arrow there, a note saying "entry". It does NOT
turn that into a strategy by itself. Observations become an **unapproved
assumption block** that the client must confirm, because inferring a rule from a
picture and then trading it would be the agent deciding the strategy, which
hard rule 1 forbids.

With no vision model configured, :func:`read_charts` still extracts everything
mechanically available and says plainly that it cannot see the drawing. It never
guesses at the contents of an image it has not read.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from ee_agent.cost.notifier import ledger
from ee_agent.secrets.vault import get_secret

SUPPORTED = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

EXTRACTION_PROMPT = """You are reading a trading chart screenshot that a trader has marked up by hand.

Report ONLY what is actually drawn or visible. Do not infer a trading strategy,
do not suggest one, and do not fill in anything you cannot see.

Return strict JSON, no prose, with this shape:

{
  "symbol": "the ticker if it is visible on the chart, else null",
  "timeframe": "the chart timeframe if visible, e.g. '5m', else null",
  "session_times": ["any times or time ranges visible or annotated"],
  "horizontal_levels": [{"label": "what the trader called it or null",
                          "price": "the price if legible, else null",
                          "note": "what it appears to mark"}],
  "arrows": [{"direction": "up|down", "where": "where on the chart it points",
               "label": "any text attached to it"}],
  "zones": [{"label": "...", "note": "a shaded or boxed region and what it marks"}],
  "text_annotations": ["every piece of handwritten or typed text on the chart"],
  "drawn_tools": ["trendline", "fib", "rectangle", ...],
  "what_the_markup_seems_to_mark": "one sentence, strictly descriptive",
  "unreadable": ["anything present but too small or unclear to read"]
}

If the image is not a trading chart, return {"not_a_chart": true, "what_it_is": "..."}.
"""


@dataclass
class ChartObservation:
    """What was actually seen on one screenshot. Descriptive, never prescriptive."""

    path: str
    symbol: str | None = None
    timeframe: str | None = None
    session_times: list[str] = field(default_factory=list)
    horizontal_levels: list[dict] = field(default_factory=list)
    arrows: list[dict] = field(default_factory=list)
    zones: list[dict] = field(default_factory=list)
    text_annotations: list[str] = field(default_factory=list)
    drawn_tools: list[str] = field(default_factory=list)
    summary: str = ""
    unreadable: list[str] = field(default_factory=list)
    read_by: str = "filename-only"
    note: str = ""
    error: str = ""

    @property
    def was_seen(self) -> bool:
        return self.read_by not in ("filename-only", "failed")

    def to_dict(self) -> dict:
        return asdict(self)

    def lines(self) -> list[str]:
        out = [f"  {Path(self.path).name}  [read by {self.read_by}]"]
        if self.symbol or self.timeframe:
            out.append(f"      {self.symbol or '?'} on {self.timeframe or '?'}")
        for level in self.horizontal_levels:
            out.append(
                f"      level {level.get('price') or '?'}"
                + (f" -- {level.get('label') or level.get('note') or ''}").rstrip()
            )
        for arrow in self.arrows:
            out.append(f"      arrow {arrow.get('direction', '?')} at {arrow.get('where', '?')}"
                       + (f" '{arrow['label']}'" if arrow.get("label") else ""))
        for zone in self.zones:
            out.append(f"      zone {zone.get('label') or ''} -- {zone.get('note') or ''}")
        for text in self.text_annotations:
            out.append(f'      text: "{text}"')
        if self.summary:
            out.append(f"      reads as: {self.summary}")
        if self.unreadable:
            out.append(f"      could not read: {', '.join(self.unreadable)}")
        if self.note:
            out.append(f"      {self.note}")
        if self.error:
            out.append(f"      FAILED: {self.error}")
        return out


@dataclass
class ScreenshotIntake:
    observations: list[ChartObservation] = field(default_factory=list)
    vision_model: str = "none"
    cost_usd: float = 0.0

    @property
    def seen(self) -> list[ChartObservation]:
        return [o for o in self.observations if o.was_seen]

    def to_dict(self) -> dict:
        return {
            "vision_model": self.vision_model,
            "cost_usd": round(self.cost_usd, 6),
            "images": len(self.observations),
            "images_read": len(self.seen),
            "observations": [o.to_dict() for o in self.observations],
        }

    def summary(self) -> str:
        head = (
            f"[screenshots] {len(self.observations)} image(s), {len(self.seen)} actually read "
            f"by {self.vision_model}"
        )
        lines = [head]
        for observation in self.observations:
            lines.extend(observation.lines())
        if not self.seen:
            lines.append(
                "      No vision model configured, so I can see the files but not what is drawn on "
                "them. Add a key (`ee-agent secrets set ANTHROPIC_API_KEY`) or just tell me what "
                "the arrows mean -- describing it out loud is the default path anyway."
            )
        else:
            lines.append(
                "      This is what I SAW, not a strategy. Nothing here becomes a rule until you "
                "confirm it."
            )
        return "\n".join(lines)


# ============================================================== vision call
def _encode(path: Path) -> tuple[str, str]:
    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
    }[path.suffix.lower()]
    return media, base64.b64encode(path.read_bytes()).decode("ascii")


def _parse_json(text: str) -> dict:
    """Models wrap JSON in prose or fences more often than not."""
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        brace = re.search(r"\{.*\}", cleaned, re.S)
        if brace:
            try:
                return json.loads(brace.group(0))
            except json.JSONDecodeError:
                pass
    raise ValueError(f"the vision model did not return JSON: {text[:200]}")


class VisionReader:
    """Reads chart markup. One method, three providers, injectable for tests."""

    def __init__(self, provider: str | None = None, model: str | None = None):
        self.provider = provider or _first_available()
        self.model = model or {
            "anthropic": "claude-opus-5",
            "openai": "gpt-4o",
            "gemini": "gemini-2.0-flash",
            "none": "none",
        }[self.provider]

    @property
    def available(self) -> bool:
        return self.provider != "none"

    @property
    def name(self) -> str:
        return "none" if not self.available else f"{self.provider}:{self.model}"

    def read(self, path: Path) -> dict:
        media, encoded = _encode(path)
        if self.provider == "anthropic":
            return self._anthropic(media, encoded)
        if self.provider == "openai":
            return self._openai(media, encoded)
        if self.provider == "gemini":
            return self._gemini(path)
        raise RuntimeError("no vision model configured")

    def _anthropic(self, media: str, encoded: str) -> dict:
        import anthropic

        client = anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model=self.model,
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media, "data": encoded}},
                        {"type": "text", "text": EXTRACTION_PROMPT},
                    ],
                }
            ],
        )
        return _parse_json("".join(b.text for b in response.content if b.type == "text"))

    def _openai(self, media: str, encoded: str) -> dict:
        from openai import OpenAI

        client = OpenAI(api_key=get_secret("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model=self.model,
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": EXTRACTION_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:{media};base64,{encoded}"}},
                    ],
                }
            ],
        )
        return _parse_json(response.choices[0].message.content or "")

    def _gemini(self, path: Path) -> dict:
        import google.generativeai as genai

        genai.configure(api_key=get_secret("GEMINI_API_KEY"))
        model = genai.GenerativeModel(self.model)
        uploaded = genai.upload_file(str(path))
        response = model.generate_content([uploaded, EXTRACTION_PROMPT])
        return _parse_json(response.text or "")


def _first_available() -> str:
    for provider, secret in (
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("gemini", "GEMINI_API_KEY"),
    ):
        if get_secret(secret):
            return provider
    return "none"


# ================================================================== intake
def read_charts(paths: list[str | Path], reader: VisionReader | None = None) -> ScreenshotIntake:
    """Read every screenshot the client uploaded."""
    reader = reader or VisionReader()
    intake = ScreenshotIntake(vision_model=reader.name)
    spend_before = ledger().total_usd

    for raw in paths:
        path = Path(raw)
        observation = ChartObservation(path=str(path))
        if not path.exists():
            observation.error = "file not found"
            observation.read_by = "failed"
            intake.observations.append(observation)
            continue
        if path.suffix.lower() not in SUPPORTED:
            observation.error = f"{path.suffix} is not an image I can read ({', '.join(sorted(SUPPORTED))})"
            observation.read_by = "failed"
            intake.observations.append(observation)
            continue

        _fill_from_filename(observation, path)

        if not reader.available:
            observation.note = (
                "I can see this file but not what is drawn on it -- that needs a vision model."
            )
            intake.observations.append(observation)
            continue

        # Metered here rather than inside the provider's read(), so a different
        # or substituted reader cannot end up unmetered. Vision tokens for image
        # reading are the largest variable cost in the system (Section 7).
        ledger().spend("visual_confirmation", **{"vision.image": 1})
        try:
            payload = reader.read(path)
        except Exception as exc:
            observation.read_by = "failed"
            observation.error = f"{type(exc).__name__}: {exc}"
            intake.observations.append(observation)
            continue

        if payload.get("not_a_chart"):
            observation.read_by = reader.name
            observation.note = f"this is not a trading chart: {payload.get('what_it_is', 'unclear')}"
            intake.observations.append(observation)
            continue

        observation.read_by = reader.name
        observation.symbol = payload.get("symbol") or observation.symbol
        observation.timeframe = payload.get("timeframe") or observation.timeframe
        observation.session_times = list(payload.get("session_times") or [])
        observation.horizontal_levels = list(payload.get("horizontal_levels") or [])
        observation.arrows = list(payload.get("arrows") or [])
        observation.zones = list(payload.get("zones") or [])
        observation.text_annotations = list(payload.get("text_annotations") or [])
        observation.drawn_tools = list(payload.get("drawn_tools") or [])
        observation.summary = payload.get("what_the_markup_seems_to_mark", "") or ""
        observation.unreadable = list(payload.get("unreadable") or [])
        intake.observations.append(observation)

    intake.cost_usd = ledger().total_usd - spend_before
    return intake


def _fill_from_filename(observation: ChartObservation, path: Path) -> None:
    """Everything mechanically available, with no model at all."""
    tokens = [t for t in re.split(r"[\s_\-.]+", path.stem) if t]
    for token in tokens:
        if re.fullmatch(r"(MNQ|NQ|ES|MES|CL|SPY|QQQ|BTCUSDT|ETHUSDT|EURUSD)", token, re.I):
            observation.symbol = token.upper()
            break
    for token in tokens:
        match = re.fullmatch(r"(\d+)\s*(m|min|h|hour)", token, re.I)
        if match:
            observation.timeframe = (
                f"{match.group(1)}{'m' if match.group(2).lower().startswith('m') else 'h'}"
            )
            break
    for stamp in re.findall(r"\d{4}[-_]\d{2}[-_]\d{2}", path.stem):
        observation.session_times.append(stamp.replace("_", "-"))


# ============================================================ spec proposal
def spec_from_screenshots(
    intake: ScreenshotIntake, strategy_id: str = "from-screenshots-v1", timezone: str = "America/Chicago"
):
    """Turn observations into a spec PROPOSAL with everything unapproved.

    The agent has not decided anything here. It has looked at the pictures and
    written down questions. Every inferred element is an assumption with
    ``approved_by: pending``, which the validator treats as blocking, so nothing
    reaches live capital on a guess from an image.
    """
    from ee_agent.spec.model import (
        Assumption, Costs, Execution, Filters, Risk, Signals, Sizing, StrategySpec, TimeWindow, Universe,
    )

    symbols = sorted({o.symbol for o in intake.observations if o.symbol}) or ["MNQ"]
    timeframes = [o.timeframe for o in intake.observations if o.timeframe]
    times = sorted({t for o in intake.observations for t in o.session_times if ":" in t})

    spec = StrategySpec(
        id=strategy_id,
        name="Proposal read from your screenshots",
        universe=Universe(instruments=symbols[:4], timezone=timezone),
        filters=Filters(
            time_windows=(
                [TimeWindow(start=times[0], end=times[-1], tz=timezone)] if len(times) >= 2 else []
            )
        ),
        risk=Risk(size=Sizing("fixed_contracts", 1)),
        execution=Execution(),
        costs=Costs(commission_per_side=0.0, slippage_ticks=1.0, spread_ticks=1.0),
        signals=Signals(),
    )

    assumptions = [
        Assumption(
            id="screenshot:entry_rule",
            question=(
                "Your screenshots show WHERE you entered but not WHY. What is the actual trigger?"
            ),
            resolution="unknown -- no entry rule can be read off a picture",
            approved_by="pending",
            sensitivity={"if_wrong": "there is no strategy to compile until you answer this"},
        )
    ]

    levels = [level for o in intake.observations for level in o.horizontal_levels]
    if levels:
        assumptions.append(
            Assumption(
                id="screenshot:levels",
                question=(
                    f"I can see {len(levels)} horizontal level(s) drawn. Are these a session range, "
                    "prior highs and lows, or something else?"
                ),
                resolution="; ".join(
                    f"{level.get('price') or '?'} ({level.get('label') or level.get('note') or 'unlabelled'})"
                    for level in levels[:6]
                ),
                approved_by="pending",
                alternatives=["session range", "prior day high/low", "swing points", "round numbers"],
                sensitivity={"if_wrong": "the levels the whole setup keys off would be the wrong ones"},
            )
        )

    arrows = [arrow for o in intake.observations for arrow in o.arrows]
    if arrows:
        ups = sum(1 for a in arrows if str(a.get("direction", "")).lower().startswith("u"))
        assumptions.append(
            Assumption(
                id="screenshot:direction",
                question=(
                    f"I count {ups} up arrow(s) and {len(arrows) - ups} down. Does this trade both "
                    "directions, or was one of those something else?"
                ),
                resolution=f"{ups} long, {len(arrows) - ups} short, as drawn",
                approved_by="pending",
                alternatives=["long only", "short only", "both"],
                sensitivity={"if_wrong": "half the strategy would be missing or invented"},
            )
        )

    notes = [text for o in intake.observations for text in o.text_annotations]
    if notes:
        assumptions.append(
            Assumption(
                id="screenshot:annotations",
                question="Your notes on the charts say the following. Which of these are rules?",
                resolution=" | ".join(notes[:10]),
                approved_by="pending",
                sensitivity={"if_wrong": "a note might be a rule or might be an aside; only you know"},
            )
        )

    if len(timeframes) > 1 and len(set(timeframes)) > 1:
        assumptions.append(
            Assumption(
                id="screenshot:timeframe",
                question=f"The screenshots span {', '.join(sorted(set(timeframes)))}. Which one do you trade?",
                resolution=f"mixed: {', '.join(sorted(set(timeframes)))}",
                approved_by="pending",
                sensitivity={"if_wrong": "the signal timeframe changes every fill price"},
            )
        )

    assumptions.append(
        Assumption(
            id="screenshot:risk",
            question="A picture cannot show me your stop and target. What are they?",
            resolution="unknown",
            approved_by="pending",
            sensitivity={"if_wrong": "I will not run a backtest with no stop"},
        )
    )
    spec.assumptions = assumptions
    spec.notes = (
        f"Read from {len(intake.observations)} screenshot(s) by {intake.vision_model}. "
        "Every element here is an UNAPPROVED assumption: nothing was inferred into a rule. "
        "Work through the assumptions with the client, then the interrogation engine finishes the job."
    )
    return spec


def questions_for(intake: ScreenshotIntake) -> list[str]:
    """What to ask the client next, in their language, based on what was seen."""
    spec = spec_from_screenshots(intake)
    return [f"[{a.id}] {a.question}" for a in spec.assumptions]
