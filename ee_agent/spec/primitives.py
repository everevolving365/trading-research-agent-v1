"""The condition vocabulary.

Section 4: *"The condition vocabulary -- session_range, range_break,
return_inside and so on -- is a registry of named primitives. Each primitive has
exactly one implementation per target: Python, Pine indicator, Pine strategy,
live. Adding a primitive means adding all four at once, and the parity harness
enforces it."*

That enforcement is real: :func:`audit_registry` fails if any primitive is
missing a target, and ``tests/test_primitives.py`` runs it.

Causality rule for every Python implementation
----------------------------------------------
A primitive computes its boolean series in :meth:`Node.prepare` with an explicit
forward loop, using only bars at or before each index. No primitive may use a
centred window, a backward fill, or a whole-array aggregate that spans the
future. The lookahead detector (``ee_agent.engine.lookahead``) verifies this
independently by recomputing on truncated data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from ee_agent.errors import PrimitiveError


# --------------------------------------------------------------------- runtime
class Runtime:
    """Everything a compiled node can see. Carries no future information."""

    def __init__(self, bars, instrument=None, spec=None, strategy_tz: str | None = None):
        self.bars = bars
        self.instrument = instrument
        self.spec = spec
        #: The live path rebuilds the plan from JSON and has no spec object, so
        #: it passes the timezone explicitly. Without this it would silently
        #: fall back to the exchange timezone and diverge from the other three
        #: targets on any instrument whose exchange is not in the strategy's tz.
        self._strategy_tz = strategy_tz
        self.contexts: dict[str, dict[str, np.ndarray]] = {}
        self.scratch: dict[str, Any] = {}
        self._calendars: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    @property
    def n(self) -> int:
        return len(self.bars)

    def calendar(self, tz: str | None) -> tuple[np.ndarray, np.ndarray]:
        """(minute_of_day, local_date) in the STRATEGY's timezone.

        A window of "09:30 to 15:00 Central" means Central, whichever instrument
        it is evaluated on. Reading the bars' own local time instead is correct
        for MNQ (Chicago) and wrong for SPY (New York) -- exactly the kind of
        per-asset drift hard rule 5 exists to prevent, and it showed up as a
        Pine/Python parity divergence on SPY before this existed.

        Use :meth:`session_days` for anything that RESETS daily. Clock
        comparisons follow the strategy's timezone; day boundaries follow the
        exchange's, because that is what Pine's ``time("D")`` does and a daily
        reset that disagreed with it would diverge every session.
        """
        tz = tz or self.strategy_tz
        if tz not in self._calendars:
            if tz == self.bars.tz:
                self._calendars[tz] = (self.bars.minute_of_day, self.bars.local_date)
            else:
                local = self.bars.df["ts"].dt.tz_convert(tz)
                self._calendars[tz] = (
                    (local.dt.hour * 60 + local.dt.minute).to_numpy(dtype=np.int32),
                    local.dt.strftime("%Y-%m-%d").to_numpy(),
                )
        return self._calendars[tz]

    def session_days(self) -> np.ndarray:
        """Exchange-local trading dates -- the unit a daily reset counts in."""
        return self.bars.local_date

    @property
    def strategy_tz(self) -> str:
        if self._strategy_tz:
            return self._strategy_tz
        if self.spec is not None and getattr(self.spec, "universe", None) is not None:
            return self.spec.universe.timezone or self.bars.tz
        return self.bars.tz


class Node:
    """A compiled condition. ``series`` is a causal boolean array."""

    def __init__(self, params: dict, env: "CompileEnv"):
        self.params = params
        self.env = env
        self.series: np.ndarray | None = None

    def prepare(self, rt: Runtime) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def value(self, rt: Runtime, i: int) -> bool:
        assert self.series is not None, "prepare() must run before value()"
        return bool(self.series[i])


class ContextNode(Node):
    """Writes named arrays into ``rt.contexts[id]`` for other nodes to read."""

    def value(self, rt: Runtime, i: int) -> bool:  # pragma: no cover
        return True


@dataclass
class PineFragment:
    """Pine v5 emission: global declarations plus a boolean expression."""

    decls: list[str] = field(default_factory=list)
    expr: str = "true"
    plots: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)


@dataclass
class CompileEnv:
    """Shared state during compilation: the spec, the instrument, and the alias
    counter that keeps generated Pine identifiers unique and deterministic."""

    spec: Any = None
    instrument: Any = None
    counter: dict[str, int] = field(default_factory=dict)
    rule_side: str | None = None
    rule_conditions: list = field(default_factory=list)

    def alias(self, stem: str) -> str:
        n = self.counter.get(stem, 0)
        self.counter[stem] = n + 1
        return f"{stem}_{n}"

    def sibling_param(self, primitive_type: str, key: str, default=None):
        """Let a condition inherit a parameter from a sibling in the same rule --
        how ``return_inside`` learns which side of the range was broken."""
        for cond in self.rule_conditions:
            if getattr(cond, "type", None) == primitive_type:
                v = cond.params.get(key)
                if v is not None:
                    return v
        return default


@dataclass
class Primitive:
    name: str
    kind: str  # "context" | "condition"
    doc: str
    params: dict[str, Any]
    python: Callable[[dict, CompileEnv], Node]
    pine_indicator: Callable[[dict, str, CompileEnv], PineFragment]
    pine_strategy: Callable[[dict, str, CompileEnv], PineFragment]
    live: Callable[[dict, CompileEnv], dict]
    flow: bool = False  # needs order-flow data, degrades to a proxy
    #: A condition whose Pine state is keyed by its parameters rather than by
    #: the rule it sits in (a structure level is the same level whichever rule
    #: reads it). Emitted once; later uses reuse the same expression.
    shared: bool = False


REGISTRY: dict[str, Primitive] = {}


def register(p: Primitive) -> Primitive:
    REGISTRY[p.name] = p
    return p


def get(name: str) -> Primitive:
    if name not in REGISTRY:
        raise PrimitiveError(
            f"Unknown primitive '{name}'. Known: {', '.join(sorted(REGISTRY))}. "
            "A new primitive must be added to all four targets at once."
        )
    return REGISTRY[name]


def audit_registry() -> list[str]:
    """Return a list of violations of the four-target rule. Empty means clean."""
    problems = []
    for name, p in REGISTRY.items():
        for target in ("python", "pine_indicator", "pine_strategy", "live"):
            if getattr(p, target, None) is None:
                problems.append(f"{name}: missing {target}")
        if p.kind not in ("context", "condition"):
            problems.append(f"{name}: bad kind {p.kind}")
    return problems


# ------------------------------------------------------------------- helpers
def _tw_minutes(hhmm: str) -> int:
    hh, mm = hhmm.split(":") if ":" in hhmm else (hhmm[:2], hhmm[2:])
    return int(hh) * 60 + int(mm)


def _pine_session(start: str, end: str) -> str:
    """'08:30','09:30' -> '0830-0930:1234567' (Pine session string)."""
    return f"{start.replace(':', '')}-{end.replace(':', '')}:1234567"


def _ema(values: np.ndarray, length: int) -> np.ndarray:
    """EMA seeded the way Pine seeds it.

    Pine's ``ta.ema`` seeds with a simple average of the first ``length`` bars
    and only then applies the alpha recursion. Seeding from ``values[0]``
    instead -- the other common convention -- leaves a residual difference that
    decays but never vanishes, and it moved a cross by two bars on the first
    spec that used ma_cross. The parity harness caught it. Anything here that
    Pine also computes must match Pine exactly, not approximately.
    """
    out = np.full(len(values), np.nan)
    if len(values) < length or length < 1:
        return out
    alpha = 2.0 / (length + 1.0)
    acc = float(np.mean(values[:length]))
    out[length - 1] = acc
    for i in range(length, len(values)):
        acc = alpha * values[i] + (1 - alpha) * acc
        out[i] = acc
    return out


def _sma(values: np.ndarray, length: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) < length:
        return out
    csum = np.cumsum(np.insert(values, 0, 0.0))
    out[length - 1 :] = (csum[length:] - csum[:-length]) / length
    return out


def _rma(values: np.ndarray, length: int) -> np.ndarray:
    """Wilder's smoothing -- what Pine's ta.rma (and therefore ta.atr/ta.rsi) uses."""
    out = np.full(len(values), np.nan)
    if len(values) < length:
        return out
    acc = float(np.mean(values[:length]))
    out[length - 1] = acc
    for i in range(length, len(values)):
        acc = (acc * (length - 1) + values[i]) / length
        out[i] = acc
    return out


def _true_range(bars) -> np.ndarray:
    prev_close = np.concatenate(([bars.close[0]], bars.close[:-1]))
    return np.maximum(
        bars.high - bars.low,
        np.maximum(np.abs(bars.high - prev_close), np.abs(bars.low - prev_close)),
    )


def atr_series(bars, length: int) -> np.ndarray:
    return _rma(_true_range(bars), length)


# =============================================================== 1. session_range
class SessionRangeNode(ContextNode):
    """High/low of a daily time window, accumulated causally.

    During the window the range is partial and grows bar by bar. After the
    window closes it is final for that day. Before the first window bar it is
    NaN. This is exactly what the Pine emission does.
    """

    def prepare(self, rt: Runtime) -> None:
        bars = rt.bars
        window = self.params.get("window", {}) or {}
        start = _tw_minutes(str(window.get("start", "08:30")))
        end = _tw_minutes(str(window.get("end", "09:30")))
        minute_of_day, _tz_date = rt.calendar(self.params.get("tz"))
        local_date = rt.session_days()
        n = len(bars)
        rhigh = np.full(n, np.nan)
        rlow = np.full(n, np.nan)
        in_win = np.zeros(n, dtype=bool)
        complete = np.zeros(n, dtype=bool)

        cur_day = None
        acc_hi = np.nan
        acc_lo = np.nan
        for i in range(n):
            day = local_date[i]
            if day != cur_day:
                cur_day, acc_hi, acc_lo = day, np.nan, np.nan
            mod = minute_of_day[i]
            inside = start <= mod < end
            if inside:
                acc_hi = bars.high[i] if np.isnan(acc_hi) else max(acc_hi, bars.high[i])
                acc_lo = bars.low[i] if np.isnan(acc_lo) else min(acc_lo, bars.low[i])
            in_win[i] = inside
            rhigh[i] = acc_hi
            rlow[i] = acc_lo
            complete[i] = (not inside) and (mod >= end) and not np.isnan(acc_hi)

        rt.contexts[self.params["id"]] = {
            "high": rhigh,
            "low": rlow,
            "in_window": in_win,
            "complete": complete,
        }
        self.series = np.ones(n, dtype=bool)


def _session_range_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    window = params.get("window", {}) or {}
    sess = _pine_session(str(window.get("start", "08:30")), str(window.get("end", "09:30")))
    tz = (env.spec.universe.timezone if env.spec else "America/Chicago")
    cid = params["id"]
    decls = [
        f'// context {cid}: session range {sess}',
        f'{cid}_in = not na(time(timeframe.period, "{sess}", "{tz}"))',
        f"var float {cid}_high = na",
        f"var float {cid}_low  = na",
        f"var bool  {cid}_done = false",
        f"{cid}_newday = ta.change(time(\"D\")) != 0",
        f"if {cid}_newday",
        f"    {cid}_high := na",
        f"    {cid}_low  := na",
        f"    {cid}_done := false",
        f"if {cid}_in",
        f"    {cid}_high := na({cid}_high) ? high : math.max({cid}_high, high)",
        f"    {cid}_low  := na({cid}_low)  ? low  : math.min({cid}_low,  low)",
        f"if not {cid}_in and not na({cid}_high)",
        f"    {cid}_done := true",
    ]
    plots = [
        f'plot({cid}_done ? {cid}_high : na, "{cid} high", color.new(color.gray, 0), 1, plot.style_linebr)',
        f'plot({cid}_done ? {cid}_low  : na, "{cid} low",  color.new(color.gray, 0), 1, plot.style_linebr)',
    ]
    return PineFragment(decls=decls, expr="true", plots=plots)


register(
    Primitive(
        name="session_range",
        kind="context",
        doc="High and low of a daily time window, accumulated causally.",
        params={"id": "str", "window": "{start,end}"},
        python=lambda p, env: SessionRangeNode(p, env),
        pine_indicator=_session_range_pine,
        pine_strategy=_session_range_pine,
        live=lambda p, env: {"type": "session_range", "id": p["id"], "window": p.get("window", {})},
    )
)


# ================================================================ 2. range_break
class RangeBreakNode(Node):
    """Arms when price breaks the referenced range and stays armed for the day.

    ``confirm: wick`` arms on the extreme of the bar; ``confirm: close`` requires
    the bar to close beyond the level. Same-bar break-and-return is allowed
    because arming is evaluated on the same index as the return.
    """

    def prepare(self, rt: Runtime) -> None:
        bars = rt.bars
        ref = self.params.get("reference")
        ctx = rt.contexts.get(ref)
        if ctx is None:
            raise PrimitiveError(f"range_break references unknown context '{ref}'")
        side = self.params.get("side", "high")
        confirm = self.params.get("confirm", "wick")
        require_complete = bool(self.params.get("require_complete", True))
        local_date = rt.session_days()
        n = len(bars)
        armed = np.zeros(n, dtype=bool)
        cur_day = None
        flag = False
        for i in range(n):
            day = local_date[i]
            if day != cur_day:
                cur_day, flag = day, False
            level_hi, level_lo = ctx["high"][i], ctx["low"][i]
            ready = (not require_complete) or bool(ctx["complete"][i])
            if ready:
                px_hi = bars.close[i] if confirm == "close" else bars.high[i]
                px_lo = bars.close[i] if confirm == "close" else bars.low[i]
                if side in ("high", "either") and not np.isnan(level_hi) and px_hi > level_hi:
                    flag = True
                if side in ("low", "either") and not np.isnan(level_lo) and px_lo < level_lo:
                    flag = True
            armed[i] = flag
        self.series = armed


def _range_break_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    ref = params.get("reference")
    side = params.get("side", "high")
    confirm = params.get("confirm", "wick")
    src_hi = "close" if confirm == "close" else "high"
    src_lo = "close" if confirm == "close" else "low"
    cond_parts = []
    if side in ("high", "either"):
        cond_parts.append(f"(not na({ref}_high) and {src_hi} > {ref}_high)")
    if side in ("low", "either"):
        cond_parts.append(f"(not na({ref}_low) and {src_lo} < {ref}_low)")
    broke = " or ".join(cond_parts) or "false"
    decls = [
        f"// {alias}: break of {ref} ({side}, confirm={confirm})",
        f"var bool {alias} = false",
        f"if {ref}_newday",
        f"    {alias} := false",
        f"if {ref}_done and ({broke})",
        f"    {alias} := true",
    ]
    return PineFragment(decls=decls, expr=alias)


register(
    Primitive(
        name="range_break",
        kind="condition",
        doc="Armed once price has broken the referenced range this session.",
        params={"reference": "context id", "side": "high|low|either", "confirm": "wick|close"},
        python=lambda p, env: RangeBreakNode(p, env),
        pine_indicator=_range_break_pine,
        pine_strategy=_range_break_pine,
        live=lambda p, env: {
            "type": "range_break",
            "reference": p.get("reference"),
            "side": p.get("side", "high"),
            "confirm": p.get("confirm", "wick"),
        },
    )
)


# ============================================================== 3. return_inside
class ReturnInsideNode(Node):
    """Price *returns* back inside the referenced range after a break.

    This is an edge, not a state. "It comes back in" fires on the bar where
    price re-enters, not on every subsequent bar it happens to be inside on.
    Two ways to qualify, both allowed by default (``mode: cross``):

    * the bar swept the level with its wick and closed back inside (the
      same-bar break-and-return the owner's ruling explicitly allows), or
    * the previous bar was outside and this one is inside.

    ``mode: state`` restores the plain 'is currently inside' reading for specs
    that genuinely mean that.

    ``side`` is inherited from the sibling ``range_break`` in the same rule when
    not given explicitly -- the client says 'it comes back in', not 'it comes
    back in from the high side'.
    """

    def prepare(self, rt: Runtime) -> None:
        bars = rt.bars
        ref = self.params.get("reference")
        ctx = rt.contexts.get(ref)
        if ctx is None:
            raise PrimitiveError(f"return_inside references unknown context '{ref}'")
        side = self.params.get("side") or self.env.sibling_param("range_break", "side", "high")
        confirm = self.params.get("confirm", "close")
        mode = self.params.get("mode", "cross")
        px = bars.close if confirm == "close" else (bars.low if side == "high" else bars.high)
        hi, lo = ctx["high"], ctx["low"]

        if side == "high":
            inside = px < hi
            swept = bars.high > hi
            prev_outside = np.concatenate(([False], (bars.close[:-1] >= hi[:-1])))
        elif side == "low":
            inside = px > lo
            swept = bars.low < lo
            prev_outside = np.concatenate(([False], (bars.close[:-1] <= lo[:-1])))
        else:
            inside = (px < hi) & (px > lo)
            swept = (bars.high > hi) | (bars.low < lo)
            prev_inside = np.concatenate(([True], inside[:-1]))
            prev_outside = ~prev_inside

        valid = ~np.isnan(hi) & ~np.isnan(lo)
        series = inside if mode == "state" else (inside & (swept | prev_outside))
        self.series = np.nan_to_num(series, nan=False).astype(bool) & valid


def _return_inside_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    ref = params.get("reference")
    side = params.get("side") or env.sibling_param("range_break", "side", "high")
    confirm = params.get("confirm", "close")
    mode = params.get("mode", "cross")
    src = "close" if confirm == "close" else ("low" if side == "high" else "high")
    decls = [f"// {alias}: return inside {ref} ({side}, confirm={confirm}, mode={mode})"]
    if side == "high":
        inside = f"(not na({ref}_high) and {src} < {ref}_high)"
        edge = f"(high > {ref}_high or (not na({ref}_high[1]) and close[1] >= {ref}_high[1]))"
    elif side == "low":
        inside = f"(not na({ref}_low) and {src} > {ref}_low)"
        edge = f"(low < {ref}_low or (not na({ref}_low[1]) and close[1] <= {ref}_low[1]))"
    else:
        inside = f"(not na({ref}_high) and {src} < {ref}_high and {src} > {ref}_low)"
        edge = f"(high > {ref}_high or low < {ref}_low or not (close[1] < {ref}_high[1] and close[1] > {ref}_low[1]))"
    expr = inside if mode == "state" else f"({inside} and {edge})"
    return PineFragment(decls=decls, expr=expr)


register(
    Primitive(
        name="return_inside",
        kind="condition",
        doc="Price has returned inside the referenced range.",
        params={"reference": "context id", "side": "high|low|inherit", "confirm": "close|wick"},
        python=lambda p, env: ReturnInsideNode(p, env),
        pine_indicator=_return_inside_pine,
        pine_strategy=_return_inside_pine,
        live=lambda p, env: {
            "type": "return_inside",
            "reference": p.get("reference"),
            "side": p.get("side") or env.sibling_param("range_break", "side", "high"),
            "confirm": p.get("confirm", "close"),
        },
    )
)


# ================================================================ 4. time_window
class TimeWindowNode(Node):
    def prepare(self, rt: Runtime) -> None:
        start = _tw_minutes(str(self.params.get("start", "00:00")))
        end = _tw_minutes(str(self.params.get("end", "23:59")))
        mod, _local_date = rt.calendar(self.params.get("tz"))
        self.series = (mod >= start) & (mod < end) if start <= end else ((mod >= start) | (mod < end))


def _time_window_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    sess = _pine_session(str(params.get("start", "00:00")), str(params.get("end", "23:59")))
    tz = params.get("tz") or (env.spec.universe.timezone if env.spec else "America/Chicago")
    return PineFragment(
        decls=[f'{alias} = not na(time(timeframe.period, "{sess}", "{tz}"))'],
        expr=alias,
    )


register(
    Primitive(
        name="time_window",
        kind="condition",
        doc="Bar falls inside a local-time window.",
        params={"start": "HH:MM", "end": "HH:MM", "tz": "IANA tz"},
        python=lambda p, env: TimeWindowNode(p, env),
        pine_indicator=_time_window_pine,
        pine_strategy=_time_window_pine,
        live=lambda p, env: {
            "type": "time_window",
            "start": p.get("start"),
            "end": p.get("end"),
            "tz": p.get("tz") or (env.spec.universe.timezone if env.spec else None),
        },
    )
)


# ================================================================ 5. session_end
class SessionEndNode(Node):
    """True on the last bar of the signal window -- the flatten trigger."""

    def prepare(self, rt: Runtime) -> None:
        bars = rt.bars
        ref = self.params.get("reference", "signal_window")
        end_hhmm = self.params.get("end")
        if end_hhmm is None and self.env.spec is not None and self.env.spec.filters.time_windows:
            end_hhmm = self.env.spec.filters.time_windows[-1].end
        end = _tw_minutes(str(end_hhmm or "23:59"))
        offset = int(self.params.get("offset_minutes", 0))
        cutoff = end - offset
        mod, _tz_date = rt.calendar(self.params.get("tz"))
        local_date = rt.session_days()
        n = len(bars)
        series = np.zeros(n, dtype=bool)
        from ee_agent.data.bars import timeframe_minutes

        step = timeframe_minutes(bars.timeframe)
        series = (mod + step >= cutoff) & (mod < cutoff)
        # also flag the true last bar of each local day, whatever the clock says
        for i in range(n - 1):
            if local_date[i] != local_date[i + 1]:
                series[i] = True
        # Deliberately NOT flagging the final element of the array. "The data ran
        # out" is not a session end -- treating it as one makes the signal depend
        # on how much history was loaded, which is lookahead. The backtester
        # closes anything still open at the end of the data separately, and says
        # so in the result notes.
        self.series = series
        rt.scratch[f"session_end:{ref}"] = series


def _session_end_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    end = params.get("end")
    if end is None and env.spec is not None and env.spec.filters.time_windows:
        end = env.spec.filters.time_windows[-1].end
    end = str(end or "23:59")
    tz = env.spec.universe.timezone if env.spec else "America/Chicago"
    sess = _pine_session("00:00", end)
    return PineFragment(
        decls=[
            f'{alias}_in = not na(time(timeframe.period, "{sess}", "{tz}"))',
            f"{alias} = {alias}_in and not {alias}_in[1] == false and not (na({alias}_in[0]) ) ? false : ({alias}_in[1] and not {alias}_in)",
        ],
        expr=f"({alias} or session.islastbar)",
    )


register(
    Primitive(
        name="session_end",
        kind="condition",
        doc="Last bar of the signal window (flatten trigger).",
        params={"reference": "signal_window", "end": "HH:MM", "offset_minutes": "int"},
        python=lambda p, env: SessionEndNode(p, env),
        pine_indicator=_session_end_pine,
        pine_strategy=_session_end_pine,
        live=lambda p, env: {"type": "session_end", "reference": p.get("reference", "signal_window")},
    )
)


# =================================================================== 6. ma_cross
class MaCrossNode(Node):
    def prepare(self, rt: Runtime) -> None:
        bars = rt.bars
        fast = int(self.params.get("fast", 9))
        slow = int(self.params.get("slow", 21))
        kind = self.params.get("ma", "ema")
        direction = self.params.get("direction", "up")
        src = getattr(bars, self.params.get("source", "close"))
        f = _ema(src, fast) if kind == "ema" else _sma(src, fast)
        s = _ema(src, slow) if kind == "ema" else _sma(src, slow)
        above = f > s
        prev = np.concatenate(([False], above[:-1]))
        valid = ~np.isnan(f) & ~np.isnan(s)
        # Pine's ta.crossover needs BOTH bars valid: on the first bar where the
        # slow average exists there is no previous value to cross from, so
        # nothing fires. Treating the missing bar as "not above" fired a cross
        # there that TradingView never shows.
        prev_valid = np.concatenate(([False], valid[:-1]))
        if direction == "up":
            self.series = above & ~prev & valid & prev_valid
        elif direction == "down":
            self.series = ~above & prev & valid & prev_valid
        elif direction == "above":
            self.series = above & valid
        else:
            self.series = ~above & valid


def _ma_cross_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    fast, slow = int(params.get("fast", 9)), int(params.get("slow", 21))
    kind = "ta.ema" if params.get("ma", "ema") == "ema" else "ta.sma"
    src = params.get("source", "close")
    direction = params.get("direction", "up")
    decls = [
        f"{alias}_f = {kind}({src}, {fast})",
        f"{alias}_s = {kind}({src}, {slow})",
    ]
    expr = {
        "up": f"ta.crossover({alias}_f, {alias}_s)",
        "down": f"ta.crossunder({alias}_f, {alias}_s)",
        "above": f"({alias}_f > {alias}_s)",
        "below": f"({alias}_f < {alias}_s)",
    }[direction]
    return PineFragment(decls=decls, expr=expr)


register(
    Primitive(
        name="ma_cross",
        kind="condition",
        doc="Moving-average cross or state.",
        params={"fast": "int", "slow": "int", "ma": "ema|sma", "direction": "up|down|above|below"},
        python=lambda p, env: MaCrossNode(p, env),
        pine_indicator=_ma_cross_pine,
        pine_strategy=_ma_cross_pine,
        live=lambda p, env: {"type": "ma_cross", **p},
    )
)


# ============================================================== 7. rsi_threshold
class RsiThresholdNode(Node):
    def prepare(self, rt: Runtime) -> None:
        bars = rt.bars
        length = int(self.params.get("length", 14))
        level = float(self.params.get("level", 30))
        direction = self.params.get("direction", "below")
        delta = np.diff(bars.close, prepend=bars.close[0])
        gain = _rma(np.clip(delta, 0, None), length)
        loss = _rma(np.clip(-delta, 0, None), length)
        with np.errstate(divide="ignore", invalid="ignore"):
            rs = np.where(loss == 0, np.inf, gain / loss)
        rsi = 100 - (100 / (1 + rs))
        valid = ~np.isnan(gain) & ~np.isnan(loss)
        prev = np.concatenate(([np.nan], rsi[:-1]))
        if direction == "below":
            self.series = (rsi < level) & valid
        elif direction == "above":
            self.series = (rsi > level) & valid
        elif direction == "cross_up":
            self.series = (rsi > level) & (prev <= level) & valid
        else:
            self.series = (rsi < level) & (prev >= level) & valid
        rt.scratch[f"rsi_{length}"] = rsi


def _rsi_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    length, level = int(params.get("length", 14)), float(params.get("level", 30))
    direction = params.get("direction", "below")
    decls = [f"{alias}_v = ta.rsi(close, {length})"]
    expr = {
        "below": f"({alias}_v < {level})",
        "above": f"({alias}_v > {level})",
        "cross_up": f"ta.crossover({alias}_v, {level})",
        "cross_down": f"ta.crossunder({alias}_v, {level})",
    }[direction]
    return PineFragment(decls=decls, expr=expr)


register(
    Primitive(
        name="rsi_threshold",
        kind="condition",
        doc="RSI above/below/crossing a level.",
        params={"length": "int", "level": "float", "direction": "above|below|cross_up|cross_down"},
        python=lambda p, env: RsiThresholdNode(p, env),
        pine_indicator=_rsi_pine,
        pine_strategy=_rsi_pine,
        live=lambda p, env: {"type": "rsi_threshold", **p},
    )
)


# =============================================================== 8. atr_filter
class AtrFilterNode(Node):
    """Volatility gate, dimensionless by construction: ATR compared to its own
    longer-run average, never to a currency amount."""

    def prepare(self, rt: Runtime) -> None:
        length = int(self.params.get("length", 14))
        baseline = int(self.params.get("baseline", 100))
        lo = float(self.params.get("min_ratio", 0.0))
        hi = float(self.params.get("max_ratio", 1e9))
        atr = atr_series(rt.bars, length)
        base = _sma(np.nan_to_num(atr, nan=0.0), baseline)
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(base > 0, atr / base, np.nan)
        self.series = (ratio >= lo) & (ratio <= hi) & ~np.isnan(ratio)
        rt.scratch[f"atr_{length}"] = atr


def _atr_filter_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    length = int(params.get("length", 14))
    baseline = int(params.get("baseline", 100))
    lo, hi = float(params.get("min_ratio", 0.0)), float(params.get("max_ratio", 1e9))
    decls = [
        f"{alias}_atr = ta.atr({length})",
        f"{alias}_base = ta.sma({alias}_atr, {baseline})",
        f"{alias}_r = {alias}_base > 0 ? {alias}_atr / {alias}_base : na",
    ]
    return PineFragment(decls=decls, expr=f"(not na({alias}_r) and {alias}_r >= {lo} and {alias}_r <= {hi})")


register(
    Primitive(
        name="atr_filter",
        kind="condition",
        doc="Volatility regime gate (ATR relative to its own baseline).",
        params={"length": "int", "baseline": "int", "min_ratio": "float", "max_ratio": "float"},
        python=lambda p, env: AtrFilterNode(p, env),
        pine_indicator=_atr_filter_pine,
        pine_strategy=_atr_filter_pine,
        live=lambda p, env: {"type": "atr_filter", **p},
    )
)


# ============================================================= 9. volume_spike
class VolumeSpikeNode(Node):
    def prepare(self, rt: Runtime) -> None:
        length = int(self.params.get("length", 20))
        mult = float(self.params.get("multiple", 2.0))
        vol = rt.bars.volume
        avg = _sma(vol, length)
        prev_avg = np.concatenate(([np.nan], avg[:-1]))  # causal: compare to the average BEFORE this bar
        self.series = (vol > mult * prev_avg) & ~np.isnan(prev_avg)


def _volume_spike_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    length, mult = int(params.get("length", 20)), float(params.get("multiple", 2.0))
    decls = [f"{alias}_avg = ta.sma(volume, {length})[1]"]
    return PineFragment(decls=decls, expr=f"(not na({alias}_avg) and volume > {mult} * {alias}_avg)")


register(
    Primitive(
        name="volume_spike",
        kind="condition",
        doc="Bar volume exceeds a multiple of its trailing average.",
        params={"length": "int", "multiple": "float"},
        python=lambda p, env: VolumeSpikeNode(p, env),
        pine_indicator=_volume_spike_pine,
        pine_strategy=_volume_spike_pine,
        live=lambda p, env: {"type": "volume_spike", **p},
    )
)


# =========================================================== 10. vwap_side
class VwapSideNode(Node):
    def prepare(self, rt: Runtime) -> None:
        bars = rt.bars
        side = self.params.get("side", "above")
        tp = (bars.high + bars.low + bars.close) / 3.0
        n = len(bars)
        vwap = np.full(n, np.nan)
        cur_day, pv, vv = None, 0.0, 0.0
        for i in range(n):
            if bars.local_date[i] != cur_day:
                cur_day, pv, vv = bars.local_date[i], 0.0, 0.0
            pv += tp[i] * bars.volume[i]
            vv += bars.volume[i]
            vwap[i] = pv / vv if vv > 0 else np.nan
        self.series = (bars.close > vwap) if side == "above" else (bars.close < vwap)
        self.series = self.series & ~np.isnan(vwap)
        rt.scratch["vwap"] = vwap


def _vwap_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    side = params.get("side", "above")
    op = ">" if side == "above" else "<"
    return PineFragment(decls=[f"{alias}_v = ta.vwap(hlc3)"], expr=f"(close {op} {alias}_v)")


register(
    Primitive(
        name="vwap_side",
        kind="condition",
        doc="Price above or below session VWAP.",
        params={"side": "above|below"},
        python=lambda p, env: VwapSideNode(p, env),
        pine_indicator=_vwap_pine,
        pine_strategy=_vwap_pine,
        live=lambda p, env: {"type": "vwap_side", **p},
    )
)


# ===================================================== 11. cum_delta_cross (flow)
class CumDeltaCrossNode(Node):
    """Cumulative delta crossing a dimensionless threshold.

    Uses real bid/ask volume when the dataset carries it, and degrades to the
    bar-derived proxy (close position within the bar range, signed by volume)
    when it does not. The degradation is recorded, never hidden.
    """

    def prepare(self, rt: Runtime) -> None:
        bars = rt.bars
        from ee_agent.flow.primitives import cumulative_delta

        cd, degraded = cumulative_delta(bars)
        rt.scratch["cum_delta"] = cd
        rt.scratch["cum_delta_degraded"] = degraded
        length = int(self.params.get("length", 20))
        z = float(self.params.get("z", 1.0))
        direction = self.params.get("direction", "up")
        mean = _sma(cd, length)
        # causal rolling stdev
        n = len(cd)
        sd = np.full(n, np.nan)
        for i in range(length - 1, n):
            sd[i] = np.std(cd[i - length + 1 : i + 1])
        upper, lower = mean + z * sd, mean - z * sd
        prev = np.concatenate(([np.nan], cd[:-1]))
        if direction == "up":
            self.series = (cd > upper) & (prev <= np.concatenate(([np.nan], upper[:-1])))
        else:
            self.series = (cd < lower) & (prev >= np.concatenate(([np.nan], lower[:-1])))
        self.series = np.nan_to_num(self.series, nan=False).astype(bool)


def _cum_delta_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    length, z = int(params.get("length", 20)), float(params.get("z", 1.0))
    direction = params.get("direction", "up")
    decls = [
        f"// {alias}: cumulative delta proxy (Pine has no order flow on a standard chart)",
        f"{alias}_sign = close > open ? 1 : close < open ? -1 : 0",
        f"{alias}_cd = ta.cum({alias}_sign * volume)",
        f"{alias}_m = ta.sma({alias}_cd, {length})",
        f"{alias}_sd = ta.stdev({alias}_cd, {length})",
    ]
    expr = (
        f"ta.crossover({alias}_cd, {alias}_m + {z} * {alias}_sd)"
        if direction == "up"
        else f"ta.crossunder({alias}_cd, {alias}_m - {z} * {alias}_sd)"
    )
    return PineFragment(decls=decls, expr=expr)


register(
    Primitive(
        name="cum_delta_cross",
        kind="condition",
        doc="Cumulative delta crosses N standard deviations of its own mean.",
        params={"length": "int", "z": "float", "direction": "up|down"},
        python=lambda p, env: CumDeltaCrossNode(p, env),
        pine_indicator=_cum_delta_pine,
        pine_strategy=_cum_delta_pine,
        live=lambda p, env: {"type": "cum_delta_cross", **p},
        flow=True,
    )
)


# ========================================================= 12. absorption (flow)
class AbsorptionNode(Node):
    """Heavy volume that fails to move price -- resting liquidity absorbing it."""

    def prepare(self, rt: Runtime) -> None:
        from ee_agent.flow.primitives import absorption_score

        score, degraded = absorption_score(
            rt.bars,
            length=int(self.params.get("length", 20)),
        )
        rt.scratch["absorption"] = score
        rt.scratch["absorption_degraded"] = degraded
        threshold = float(self.params.get("threshold", 2.0))
        self.series = np.nan_to_num(score >= threshold, nan=False).astype(bool)


def _absorption_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    length = int(params.get("length", 20))
    threshold = float(params.get("threshold", 2.0))
    decls = [
        f"// {alias}: absorption proxy = volume z-score divided by range z-score",
        f"{alias}_rng = math.max(high - low, syminfo.mintick)",
        f"{alias}_vz = (volume - ta.sma(volume, {length})) / math.max(ta.stdev(volume, {length}), 1e-9)",
        f"{alias}_rz = ({alias}_rng - ta.sma({alias}_rng, {length})) / math.max(ta.stdev({alias}_rng, {length}), 1e-9)",
        f"{alias}_s = {alias}_vz - {alias}_rz",
    ]
    return PineFragment(decls=decls, expr=f"(not na({alias}_s) and {alias}_s >= {threshold})")


register(
    Primitive(
        name="absorption",
        kind="condition",
        doc="High volume with suppressed range: liquidity absorbing the move.",
        params={"length": "int", "threshold": "float"},
        python=lambda p, env: AbsorptionNode(p, env),
        pine_indicator=_absorption_pine,
        pine_strategy=_absorption_pine,
        live=lambda p, env: {"type": "absorption", **p},
        flow=True,
    )
)


# =============================================================== 13. atr context
class AtrContextNode(ContextNode):
    def prepare(self, rt: Runtime) -> None:
        length = int(self.params.get("length", 14))
        rt.contexts[self.params["id"]] = {"atr": atr_series(rt.bars, length)}
        self.series = np.ones(len(rt.bars), dtype=bool)


def _atr_ctx_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    cid, length = params["id"], int(params.get("length", 14))
    return PineFragment(decls=[f"{cid}_atr = ta.atr({length})"], expr="true")


register(
    Primitive(
        name="atr",
        kind="context",
        doc="Average true range, available to risk sizing and other conditions.",
        params={"id": "str", "length": "int"},
        python=lambda p, env: AtrContextNode(p, env),
        pine_indicator=_atr_ctx_pine,
        pine_strategy=_atr_ctx_pine,
        live=lambda p, env: {"type": "atr", "id": p["id"], "length": p.get("length", 14)},
    )
)


# =====================================================================
# 14-19. Market structure and fair value gaps (NASH Breaker Block v2)
#
# Ported from the owner's own TradingView script, which is kept verbatim in
# library/nash-breaker-block-v2/original/ and run through the Pine interpreter
# as a fifth parity target. Every rule below is that script's rule, including
# its edge cases: TradingView's pivot tie-breaking, the one-shot "burn" of a
# broken level, and the moment a 15-minute bar's values reach a 5-minute chart.
# =====================================================================
def _distance_param(value) -> tuple[str, float]:
    """A distance parameter as (unit, value). A bare number means points,
    because that is how the owner's script states it."""
    if isinstance(value, bool):
        raise PrimitiveError(f"cannot read distance {value!r}")
    if isinstance(value, (int, float)):
        return "points", float(value)
    if isinstance(value, dict):
        unit = str(value.get("type") or value.get("unit") or "points")
        return unit, float(value.get("value", 0))
    raise PrimitiveError(f"cannot read distance {value!r}: use a number of points or {{type, value}}")


def _distance_series(rt: Runtime, value) -> np.ndarray:
    unit, v = _distance_param(value)
    n = rt.n
    if unit == "points":
        return np.full(n, v)
    if unit == "ticks":
        tick = getattr(rt.instrument, "tick_size", None)
        if not tick:
            raise PrimitiveError("a distance in ticks needs the instrument's tick size")
        return np.full(n, v * float(tick))
    if unit == "atr":
        return atr_series(rt.bars, 14) * v
    if unit == "percent":
        return rt.bars.close * v / 100.0
    raise PrimitiveError(f"distance unit '{unit}' is not supported here; use points, ticks, atr or percent")


def _distance_pine(value) -> str:
    unit, v = _distance_param(value)
    if unit == "points":
        return f"{v}"
    if unit == "ticks":
        return f"{v} * syminfo.mintick"
    if unit == "atr":
        return f"ta.atr(14) * {v}"
    if unit == "percent":
        return f"close * {v} / 100.0"
    raise PrimitiveError(f"distance unit '{unit}' is not supported here; use points, ticks, atr or percent")


# ------------------------------------------------------------ structure state
def _structure_name(side: str, pivot_len: int, confirm: str) -> str:
    """Deterministic Pine name: the level is a function of these three things
    only, so two rules reading it must share one declaration."""
    return f"bos_{'hi' if side == 'high' else 'lo'}_p{int(pivot_len)}{'c' if confirm == 'close' else 'w'}"


def _structure_params(params: dict, env: CompileEnv, *, inherit: bool) -> tuple[str, int, str]:
    """(side, pivot_len, confirm) for a structure_break -- or, with ``inherit``,
    for the structure_break sitting next to this condition in the same rule."""
    if inherit:
        side = env.sibling_param("structure_break", "side")
        if side is None:
            side = {"long": "high", "short": "low"}.get(env.rule_side or "")
        if side is None:
            raise PrimitiveError(
                "fvg_fuel reads the level broken by a structure_break in the same rule, "
                "and this rule has none"
            )
        pivot_len = env.sibling_param("structure_break", "pivot_len", 3)
        confirm = env.sibling_param("structure_break", "confirm", "close")
    else:
        side = params.get("side") or {"long": "high", "short": "low"}.get(env.rule_side or "", "high")
        pivot_len = params.get("pivot_len", 3)
        confirm = params.get("confirm", "close")
    if side not in ("high", "low"):
        raise PrimitiveError(f"structure side must be high or low, got {side!r}")
    if confirm not in ("close", "wick"):
        raise PrimitiveError(f"structure confirm must be close or wick, got {confirm!r}")
    pivot_len = int(pivot_len)
    if pivot_len < 1:
        raise PrimitiveError("pivot_len must be at least 1")
    return side, pivot_len, confirm


def structure_state(rt: Runtime, side: str, pivot_len: int, confirm: str) -> tuple[np.ndarray, np.ndarray]:
    """The tracked structure level and the bars on which it breaks.

    Mirrors the owner's script line for line:

    * the level is the most recent confirmed ``ta.pivothigh(high, L, L)`` (or
      pivotlow). A pivot is confirmed L bars after it prints.
    * TradingView's tie rule: an equal bar on the LEFT does not stop a pivot, an
      equal bar on the RIGHT does -- so of two equal highs the later one is the
      pivot. (Matches ``ta.pivothigh`` as TradingView evaluates it.)
    * a new level starts "used" if the confirming bar already closed through it.
    * a break is ``close > level`` (or the high, for ``confirm: wick``) while the
      level is unused; the level is then burned, valid signal or not, so one
      level can fire at most once.
    """
    name = _structure_name(side, pivot_len, confirm)
    key = f"structure:{name}"
    cached = rt.scratch.get(key)
    if cached is not None:
        return cached
    bars = rt.bars
    high_side = side == "high"
    src = (bars.high if high_side else bars.low).tolist()
    px = (bars.close if confirm == "close" else (bars.high if high_side else bars.low)).tolist()
    close = bars.close.tolist()
    n = len(src)
    L = pivot_len
    level = np.full(n, np.nan)
    broke = np.zeros(n, dtype=bool)
    lvl = math.nan
    used = True
    for i in range(n):
        c = i - L
        if c - L >= 0:
            center = src[c]
            pivot = center == center  # not NaN
            if pivot:
                for j in range(c - L, c):
                    v = src[j]
                    if v != v or (v > center if high_side else v < center):
                        pivot = False
                        break
            if pivot:
                for j in range(c + 1, i + 1):
                    v = src[j]
                    if v != v or (v >= center if high_side else v <= center):
                        pivot = False
                        break
            if pivot:
                lvl = center
                used = close[i] > center if high_side else close[i] < center
        fired = False
        if lvl == lvl and not used:
            fired = px[i] > lvl if high_side else px[i] < lvl
        broke[i] = fired
        level[i] = lvl
        if fired:
            used = True
    rt.scratch[key] = (level, broke)
    rt.scratch[f"{name}_level"] = level
    return level, broke


# ================================================================ 14. structure_break
class StructureBreakNode(Node):
    """Break of structure: price closes through the most recent confirmed pivot."""

    def prepare(self, rt: Runtime) -> None:
        side, pivot_len, confirm = _structure_params(self.params, self.env, inherit=False)
        _level, broke = structure_state(rt, side, pivot_len, confirm)
        self.series = broke.copy()


def _structure_break_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    side, L, confirm = _structure_params(params, env, inherit=False)
    nm = _structure_name(side, L, confirm)
    high_side = side == "high"
    pivot = f"ta.pivothigh(high, {L}, {L})" if high_side else f"ta.pivotlow(low, {L}, {L})"
    cmp = ">" if high_side else "<"
    px = "close" if confirm == "close" else ("high" if high_side else "low")
    label = "structure high" if high_side else "structure low"
    decls = [
        f"// structure: latest confirmed pivot {'high' if high_side else 'low'} ({L} bars each side), "
        "burned once broken so it fires at most once",
        f"{nm}_pv = {pivot}",
        f"var float {nm}_lvl = na",
        f"var bool {nm}_used = true",
        f"if not na({nm}_pv)",
        f"    {nm}_lvl := {nm}_pv",
        f"    {nm}_used := close {cmp} {nm}_pv",
        f"{nm}_brk = not na({nm}_lvl) and not {nm}_used and {px} {cmp} {nm}_lvl",
        f"if {nm}_brk",
        f"    {nm}_used := true",
    ]
    plots = [f'plot({nm}_lvl, "{label}", color.new(color.aqua, 35), 1, plot.style_linebr)']
    return PineFragment(decls=decls, expr=f"{nm}_brk", plots=plots)


def _structure_break_live(p: dict, env: CompileEnv) -> dict:
    side, L, confirm = _structure_params(p, env, inherit=False)
    return {"type": "structure_break", "side": side, "pivot_len": L, "confirm": confirm}


register(
    Primitive(
        name="structure_break",
        kind="condition",
        doc="Break of structure: close through the latest confirmed pivot high/low, once per level.",
        params={"side": "high|low", "pivot_len": "int (bars each side)", "confirm": "close|wick"},
        python=lambda p, env: StructureBreakNode(p, env),
        pine_indicator=_structure_break_pine,
        pine_strategy=_structure_break_pine,
        live=_structure_break_live,
        shared=True,
    )
)


# ======================================================================= 15. htf_fvg
def fvg_table(rt: Runtime, timeframe: str) -> dict:
    """Every higher-timeframe fair value gap, in the order the script records them.

    A bullish gap is ``high[2] < low`` on the higher timeframe (top = low,
    bottom = high[2]); bearish is ``low[2] > high`` (top = low[2], bottom =
    high). A gap is recorded on the chart bar where that higher-timeframe bar's
    values first arrive -- see :mod:`ee_agent.data.htf`. ``mitigated_at`` is the
    first chart bar, from the creation bar on, whose range touches the gap; it is
    only ever read as "mitigated_at <= current bar", which keeps it causal.
    """
    from ee_agent.data.htf import htf_view

    key = f"fvg_table:{timeframe}"
    if key in rt.scratch:
        return rt.scratch[key]
    bars = rt.bars
    htf, kmap = htf_view(bars, timeframe)
    H, Lo = htf.high.tolist(), htf.low.tolist()
    n = len(bars)
    tops: list[float] = []
    bots: list[float] = []
    dirs: list[int] = []
    at: list[int] = []
    bull = np.zeros(n, dtype=bool)
    bear = np.zeros(n, dtype=bool)
    prev = -1
    for i in range(n):
        k = int(kmap[i])
        is_new = i == 0 or k != prev
        prev = k
        if not is_new or k < 2:
            continue
        if H[k - 2] < Lo[k]:
            tops.append(Lo[k]); bots.append(H[k - 2]); dirs.append(1); at.append(i)
            bull[i] = True
        if Lo[k - 2] > H[k]:
            tops.append(Lo[k - 2]); bots.append(H[k]); dirs.append(-1); at.append(i)
            bear[i] = True
    high, low = rt.bars.high, rt.bars.low
    mitigated_at: list[int] = []
    for top, bot, c in zip(tops, bots, at):
        hit = np.flatnonzero((high[c:] >= bot) & (low[c:] <= top))
        mitigated_at.append(c + int(hit[0]) if len(hit) else n)
    table = {
        "top": tops, "bot": bots, "dir": dirs, "bar": at, "mitigated_at": mitigated_at,
        "bull": bull, "bear": bear, "htf_index": kmap,
    }
    rt.scratch[key] = table
    return table


class HtfFvgNode(ContextNode):
    """Higher-timeframe fair value gaps, recorded as they are confirmed."""

    def prepare(self, rt: Runtime) -> None:
        tf = str(self.params.get("timeframe", "15"))
        table = fvg_table(rt, tf)
        rt.scratch[f"fvg_context:{self.params['id']}"] = table
        rt.contexts[self.params["id"]] = {
            "bull": table["bull"],
            "bear": table["bear"],
            "htf_index": table["htf_index"].astype(float),
        }
        self.series = np.ones(rt.n, dtype=bool)


def _fvg_horizon(spec, cid: str) -> int:
    """How many chart bars a gap can still be read for. The generated script
    only tracks mitigation that long -- an older gap is never read again, so the
    result is identical and TradingView's per-bar loop stays short."""
    ages = [60]
    if spec is not None:
        ages = [
            int(c.params.get("max_age", 60))
            for c in spec.all_conditions()
            if c.type == "fvg_fuel" and c.params.get("reference") == cid
        ] or [60]
    return max(ages)


def _htf_fvg_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    from ee_agent.data.htf import pine_timeframe_minutes

    cid = params["id"]
    tf = str(pine_timeframe_minutes(str(params.get("timeframe", "15"))))
    horizon = _fvg_horizon(env.spec, cid)
    c = cid
    decls = [
        f"// context {c}: {tf}-minute fair value gaps, recorded when the {tf}-minute bar closes",
        f'[{c}_h0, {c}_l0, {c}_h2, {c}_l2, {c}_bi] = request.security(syminfo.tickerid, "{tf}", '
        "[high, low, high[2], low[2], bar_index], lookahead=barmerge.lookahead_off)",
        f"{c}_new = ta.change({c}_bi) != 0",
        f"{c}_bull = {c}_new and {c}_h2 < {c}_l0",
        f"{c}_bear = {c}_new and {c}_l2 > {c}_h0",
        f"var {c}_top = array.new_float()",
        f"var {c}_bot = array.new_float()",
        f"var {c}_dir = array.new_int()",
        f"var {c}_bar = array.new_int()",
        f"var {c}_mit = array.new_bool()",
        f"if {c}_bull",
        f"    array.push({c}_top, {c}_l0)",
        f"    array.push({c}_bot, {c}_h2)",
        f"    array.push({c}_dir, 1)",
        f"    array.push({c}_bar, bar_index)",
        f"    array.push({c}_mit, false)",
        f"if {c}_bear",
        f"    array.push({c}_top, {c}_l2)",
        f"    array.push({c}_bot, {c}_h0)",
        f"    array.push({c}_dir, -1)",
        f"    array.push({c}_bar, bar_index)",
        f"    array.push({c}_mit, false)",
        f"// mitigation: price trades into the gap (tracked while a gap can still be read: {horizon} bars)",
        f"if array.size({c}_top) > 0",
        f"    for j = array.size({c}_top) - 1 to 0",
        f"        if bar_index - array.get({c}_bar, j) > {horizon}",
        "            break",
        f"        if not array.get({c}_mit, j) and high >= array.get({c}_bot, j) and low <= array.get({c}_top, j)",
        f"            array.set({c}_mit, j, true)",
        f"// is there a qualifying gap on the fuel side of a broken level?",
        f"{c}_fuel(isLong, lvl, maxDist, maxAge, matchDir, mustOpen) =>",
        "    bool found = false",
        f"    int n = array.size({c}_top)",
        "    if n > 0 and not na(lvl)",
        "        for k = 0 to n - 1",
        "            int j = n - 1 - k",
        f"            int b = array.get({c}_bar, j)",
        "            if bar_index - b > maxAge",
        "                break",
        "            if b < bar_index",
        f"                float t = array.get({c}_top, j)",
        f"                float bt = array.get({c}_bot, j)",
        f"                int d = array.get({c}_dir, j)",
        "                bool dirOk = not matchDir or (isLong ? d == 1 : d == -1)",
        f"                bool openOk = not mustOpen or not array.get({c}_mit, j)",
        "                bool posOk = isLong ? (t <= lvl and lvl - t <= maxDist) : (bt >= lvl and bt - lvl <= maxDist)",
        "                if dirOk and openOk and posOk",
        "                    found := true",
        "                    break",
        "    found",
    ]
    return PineFragment(decls=decls, expr="true")


register(
    Primitive(
        name="htf_fvg",
        kind="context",
        doc="Higher-timeframe fair value gaps (3-bar imbalances), recorded as each bar closes.",
        params={"id": "str", "timeframe": "minutes, e.g. '15'"},
        python=lambda p, env: HtfFvgNode(p, env),
        pine_indicator=_htf_fvg_pine,
        pine_strategy=_htf_fvg_pine,
        live=lambda p, env: {"type": "htf_fvg", "id": p["id"], "timeframe": str(p.get("timeframe", "15"))},
    )
)


# ====================================================================== 16. fvg_fuel
class FvgFuelNode(Node):
    """A fair value gap sits on the fuel side of the level being broken.

    For a long (a break UP through a structure high) a qualifying gap has its
    top at or below the broken level and within ``max_distance`` of it; for a
    short, its bottom at or above the level. Gaps are searched newest first,
    only those created before this bar and no more than ``max_age`` chart bars
    old. ``match_direction`` wants a bullish gap for longs and a bearish one for
    shorts; ``must_be_unmitigated`` rejects gaps price has already traded into.

    The series is "the level broke on this bar AND it had fuel": the search only
    runs on a break bar, in Python and in the emitted Pine alike (a ternary, lazy
    in every Pine version). The owner's script evaluates it the same way.
    """

    def prepare(self, rt: Runtime) -> None:
        ref = self.params.get("reference")
        table = rt.scratch.get(f"fvg_context:{ref}")
        if table is None:
            raise PrimitiveError(f"fvg_fuel references unknown fair-value-gap context '{ref}'")
        side, pivot_len, confirm = _structure_params(self.params, self.env, inherit=True)
        level, broke = structure_state(rt, side, pivot_len, confirm)
        is_long = side == "high"
        max_age = int(self.params.get("max_age", 60))
        match_dir = bool(self.params.get("match_direction", True))
        must_open = bool(self.params.get("must_be_unmitigated", False))
        dist = _distance_series(rt, self.params.get("max_distance", 120)).tolist()
        tops, bots, dirs, fbar, mit = (
            table["top"], table["bot"], table["dir"], table["bar"], table["mitigated_at"]
        )
        levels = level.tolist()
        n, m = rt.n, len(fbar)
        out = np.zeros(n, dtype=bool)
        upto = 0
        want = 1 if is_long else -1
        for i in np.flatnonzero(broke).tolist():
            while upto < m and fbar[upto] <= i:
                upto += 1
            lvl = levels[i]
            if lvl != lvl:
                continue
            max_d = dist[i]
            for j in range(upto - 1, -1, -1):
                b = fbar[j]
                if i - b > max_age:
                    break
                if b < i:
                    if match_dir and dirs[j] != want:
                        continue
                    if must_open and mit[j] <= i:
                        continue
                    if is_long:
                        ok = tops[j] <= lvl and (lvl - tops[j]) <= max_d
                    else:
                        ok = bots[j] >= lvl and (bots[j] - lvl) <= max_d
                    if ok:
                        out[i] = True
                        break
        self.series = out


def _fvg_fuel_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    ref = params.get("reference")
    side, L, confirm = _structure_params(params, env, inherit=True)
    nm = _structure_name(side, L, confirm)
    is_long = "true" if side == "high" else "false"
    max_age = int(params.get("max_age", 60))
    match = "true" if params.get("match_direction", True) else "false"
    must = "true" if params.get("must_be_unmitigated", False) else "false"
    decls = [
        f"// {alias}: a {ref} gap on the fuel side of {nm} (max age {max_age} bars)",
        f"{alias}_dist = {_distance_pine(params.get('max_distance', 120))}",
    ]
    return PineFragment(
        decls=decls,
        expr=f"({nm}_brk ? {ref}_fuel({is_long}, {nm}_lvl, {alias}_dist, {max_age}, {match}, {must}) : false)",
    )


def _fvg_fuel_live(p: dict, env: CompileEnv) -> dict:
    side, L, confirm = _structure_params(p, env, inherit=True)
    return {
        "type": "fvg_fuel",
        "reference": p.get("reference"),
        "max_distance": p.get("max_distance", 120),
        "max_age": int(p.get("max_age", 60)),
        "match_direction": bool(p.get("match_direction", True)),
        "must_be_unmitigated": bool(p.get("must_be_unmitigated", False)),
        # carried explicitly so the live path never depends on sibling lookup
        "structure": {"side": side, "pivot_len": L, "confirm": confirm},
    }


class _LiveFvgFuelNode(FvgFuelNode):
    """The live config carries the structure explicitly; honour it."""

    def prepare(self, rt: Runtime) -> None:
        s = self.params.get("structure")
        if s:
            self.env = CompileEnv(
                spec=None,
                rule_conditions=[_Cond("structure_break", dict(s))],
            )
        super().prepare(rt)


@dataclass
class _Cond:
    type: str
    params: dict


register(
    Primitive(
        name="fvg_fuel",
        kind="condition",
        doc="A higher-timeframe fair value gap sits on the fuel side of the structure level being broken.",
        params={
            "reference": "htf_fvg context id",
            "max_distance": "points (or {type: ticks|atr|percent, value})",
            "max_age": "chart bars",
            "match_direction": "bool",
            "must_be_unmitigated": "bool",
        },
        python=lambda p, env: _LiveFvgFuelNode(p, env),
        pine_indicator=_fvg_fuel_pine,
        pine_strategy=_fvg_fuel_pine,
        live=_fvg_fuel_live,
    )
)


# ================================================================== 17. htf_ema_side
class HtfEmaSideNode(Node):
    """Price above/below a higher-timeframe EMA (the script's 15m 21 EMA anchor)."""

    def prepare(self, rt: Runtime) -> None:
        from ee_agent.data.htf import htf_view

        tf = str(self.params.get("timeframe", "15"))
        length = int(self.params.get("length", 21))
        side = self.params.get("side") or ("below" if self.env.rule_side == "short" else "above")
        src_name = self.params.get("source", "close")
        htf, kmap = htf_view(rt.bars, tf)
        ema = _ema(getattr(htf, src_name), length)
        mapped = np.full(rt.n, np.nan)
        ok = kmap >= 0
        if len(ema):
            mapped[ok] = ema[kmap[ok]]
        close = rt.bars.close
        with np.errstate(invalid="ignore"):
            series = close > mapped if side == "above" else close < mapped
        self.series = series & ~np.isnan(mapped)
        rt.scratch[f"htf_ema:{tf}:{length}"] = mapped


def _htf_ema_side_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    from ee_agent.data.htf import pine_timeframe_minutes

    tf = str(pine_timeframe_minutes(str(params.get("timeframe", "15"))))
    length = int(params.get("length", 21))
    side = params.get("side") or ("below" if env.rule_side == "short" else "above")
    src = params.get("source", "close")
    op = ">" if side == "above" else "<"
    decls = [
        f'{alias}_e = request.security(syminfo.tickerid, "{tf}", ta.ema({src}, {length}), '
        "lookahead=barmerge.lookahead_off)"
    ]
    return PineFragment(decls=decls, expr=f"(not na({alias}_e) and close {op} {alias}_e)")


register(
    Primitive(
        name="htf_ema_side",
        kind="condition",
        doc="Close above/below a higher-timeframe EMA.",
        params={"timeframe": "minutes", "length": "int", "side": "above|below", "source": "close"},
        python=lambda p, env: HtfEmaSideNode(p, env),
        pine_indicator=_htf_ema_side_pine,
        pine_strategy=_htf_ema_side_pine,
        live=lambda p, env: {
            "type": "htf_ema_side",
            "timeframe": str(p.get("timeframe", "15")),
            "length": int(p.get("length", 21)),
            "side": p.get("side") or ("below" if env.rule_side == "short" else "above"),
            "source": p.get("source", "close"),
        },
    )
)


# ============================================================ 18. session_open_levels
_DEFAULT_ANCHORS = ["17:00", "23:00", "08:30"]


class SessionOpenLevelsNode(ContextNode):
    """Opening prices at fixed clock times (5pm Globex, 11pm, 8:30 NY open).

    The first anchor starts a new cycle and clears the others, as the owner's
    script does. Each anchor must fall on a chart-bar open: the script reads
    1-minute opens, and on a chart whose bars start on the anchor that is the
    same price. Where a bar does not start on it, this refuses rather than
    reading a different price.
    """

    def prepare(self, rt: Runtime) -> None:
        from ee_agent.data.bars import timeframe_minutes

        anchors = [str(a) for a in (self.params.get("anchors") or _DEFAULT_ANCHORS)]
        minutes = [_tw_minutes(a) for a in anchors]
        step = timeframe_minutes(rt.bars.timeframe)
        for a, m in zip(anchors, minutes):
            if step >= 1 and m % int(step) != 0:
                raise PrimitiveError(
                    f"anchor {a} does not fall on a {rt.bars.timeframe} bar open; use a chart "
                    "timeframe that divides it (1m, 5m, 15m, 30m for 08:30)"
                )
        mod, _d = rt.calendar(self.params.get("tz"))
        n = rt.n
        opens = rt.bars.open
        cur = [math.nan] * len(minutes)
        out = [np.full(n, np.nan) for _ in minutes]
        for i in range(n):
            m = int(mod[i])
            for idx, anchor in enumerate(minutes):
                if m == anchor:
                    cur[idx] = float(opens[i])
                    if idx == 0:
                        for other in range(1, len(cur)):
                            cur[other] = math.nan
            for idx in range(len(minutes)):
                out[idx][i] = cur[idx]
        rt.contexts[self.params["id"]] = {f"level_{k}": arr for k, arr in enumerate(out)}
        self.series = np.ones(n, dtype=bool)


def _session_open_levels_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    cid = params["id"]
    anchors = [str(a) for a in (params.get("anchors") or _DEFAULT_ANCHORS)]
    tz = params.get("tz") or (env.spec.universe.timezone if env.spec else "America/Chicago")
    decls = [
        f"// context {cid}: opening prices at {', '.join(anchors)} {tz}",
        f'{cid}_hm = hour(time, "{tz}") * 60 + minute(time, "{tz}")',
    ]
    for k in range(len(anchors)):
        decls.append(f"var float {cid}_{k} = na")
    for k, a in enumerate(anchors):
        decls.append(f"if {cid}_hm == {_tw_minutes(a)}")
        decls.append(f"    {cid}_{k} := open")
        if k == 0:
            for other in range(1, len(anchors)):
                decls.append(f"    {cid}_{other} := na")
    plots = [f'plot({cid}_{k}, "{cid} {a}", color.new(color.orange, 0), 1, plot.style_linebr)' for k, a in enumerate(anchors)]
    return PineFragment(decls=decls, expr="true", plots=plots)


register(
    Primitive(
        name="session_open_levels",
        kind="context",
        doc="Opening prices at fixed clock times; the first anchor starts a new cycle.",
        params={"id": "str", "anchors": "[HH:MM, ...]", "tz": "IANA tz"},
        python=lambda p, env: SessionOpenLevelsNode(p, env),
        pine_indicator=_session_open_levels_pine,
        pine_strategy=_session_open_levels_pine,
        live=lambda p, env: {
            "type": "session_open_levels",
            "id": p["id"],
            "anchors": [str(a) for a in (p.get("anchors") or _DEFAULT_ANCHORS)],
            "tz": p.get("tz") or (env.spec.universe.timezone if env.spec else None),
        },
    )
)


# ================================================================ 19. open_level_break
class OpenLevelBreakNode(Node):
    """Price breaks through any of the session open levels on this bar.

    ``confirm: close`` -- previous close below, this close above (for up).
    ``confirm: wick``  -- previous close at or below, this high above.
    """

    def prepare(self, rt: Runtime) -> None:
        ref = self.params.get("reference")
        ctx = rt.contexts.get(ref)
        if ctx is None:
            raise PrimitiveError(f"open_level_break references unknown context '{ref}'")
        direction = self.params.get("direction") or ("down" if self.env.rule_side == "short" else "up")
        confirm = self.params.get("confirm", "wick")
        bars = rt.bars
        prev = np.concatenate(([np.nan], bars.close[:-1]))
        out = np.zeros(rt.n, dtype=bool)
        with np.errstate(invalid="ignore"):
            for key in sorted(k for k in ctx if k.startswith("level_")):
                lv = ctx[key]
                if direction == "up":
                    hit = (prev < lv) & (bars.close > lv) if confirm == "close" else (prev <= lv) & (bars.high > lv)
                else:
                    hit = (prev > lv) & (bars.close < lv) if confirm == "close" else (prev >= lv) & (bars.low < lv)
                out |= hit & ~np.isnan(lv)
        self.series = out


def _open_level_break_pine(params: dict, alias: str, env: CompileEnv) -> PineFragment:
    ref = params.get("reference")
    direction = params.get("direction") or ("down" if env.rule_side == "short" else "up")
    confirm = params.get("confirm", "wick")
    anchors = _DEFAULT_ANCHORS
    if env.spec is not None:
        for c in env.spec.context:
            if c.id == ref:
                anchors = [str(a) for a in (c.params.get("anchors") or _DEFAULT_ANCHORS)]
    parts = []
    for k in range(len(anchors)):
        lv = f"{ref}_{k}"
        if direction == "up":
            body = f"close[1] < {lv} and close > {lv}" if confirm == "close" else f"close[1] <= {lv} and high > {lv}"
        else:
            body = f"close[1] > {lv} and close < {lv}" if confirm == "close" else f"close[1] >= {lv} and low < {lv}"
        parts.append(f"(not na({lv}) and {body})")
    return PineFragment(decls=[f"{alias} = {' or '.join(parts) or 'false'}"], expr=alias)


register(
    Primitive(
        name="open_level_break",
        kind="condition",
        doc="Price breaks through one of the session open levels on this bar.",
        params={"reference": "session_open_levels id", "direction": "up|down", "confirm": "close|wick"},
        python=lambda p, env: OpenLevelBreakNode(p, env),
        pine_indicator=_open_level_break_pine,
        pine_strategy=_open_level_break_pine,
        live=lambda p, env: {
            "type": "open_level_break",
            "reference": p.get("reference"),
            "direction": p.get("direction") or ("down" if env.rule_side == "short" else "up"),
            "confirm": p.get("confirm", "wick"),
        },
    )
)


def primitive_names() -> list[str]:
    return sorted(REGISTRY)


def flow_primitives() -> list[str]:
    return sorted(n for n, p in REGISTRY.items() if p.flow)
