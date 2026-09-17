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
    out = np.full(len(values), np.nan)
    if len(values) == 0:
        return out
    alpha = 2.0 / (length + 1.0)
    acc = values[0]
    out[0] = acc
    for i in range(1, len(values)):
        acc = alpha * values[i] + (1 - alpha) * acc
        out[i] = acc
    out[: length - 1] = np.nan
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
        if direction == "up":
            self.series = above & ~prev & valid
        elif direction == "down":
            self.series = ~above & prev & valid
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


def primitive_names() -> list[str]:
    return sorted(REGISTRY)


def flow_primitives() -> list[str]:
    return sorted(n for n, p in REGISTRY.items() if p.flow)
