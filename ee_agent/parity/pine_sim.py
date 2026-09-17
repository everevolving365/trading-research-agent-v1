"""A Pine v5 interpreter for the subset this project emits.

Why this exists
---------------
Law Two says the Python backtest, the Pine indicator, the Pine strategy and the
live config are the same object, provably. Re-running the Python plan and
calling that "parity" proves nothing -- it compares a thing to itself.

So the parity harness parses and executes the **emitted Pine source text**, bar
by bar, with Pine's own series semantics, and compares the resulting signal
series against the Python engine's. A divergence is located to the bar.

What it does and does not prove
-------------------------------
It proves the emitted script computes the same signals as the Python engine on
the same bars, under a faithful implementation of the Pine constructs used.
It does not prove TradingView's servers agree -- only TradingView can do that,
which is what ability 46 (the Operator's deep backtest cross-check) is for.
The Python engine remains authoritative (Section 9, honest limit 1).

The supported subset is closed: this interpreter only has to run code this
project generates, and ``ee_agent.parity.harness`` fails loudly on any construct
it does not recognise rather than skipping it.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

NA = float("nan")


class PineError(Exception):
    """The interpreter met something it does not implement. Never silent."""


# ============================================================ expression model
@dataclass
class Node:
    kind: str
    value: Any = None
    children: list["Node"] = field(default_factory=list)
    site: int = -1  # unique call-site id, for stateful ta.* functions


TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<str>"[^"]*")
  | (?P<num>\d+\.\d+|\.\d+|\d+)
  | (?P<name>[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)
  | (?P<op>:=|==|!=|<=|>=|\+=|-=|\?|:|\(|\)|\[|\]|,|\+|-|\*|/|%|<|>|=)
    """,
    re.VERBOSE,
)

KEYWORDS = {"and", "or", "not", "var", "if", "else", "true", "false", "na", "for", "while"}


def tokenize(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m:
            raise PineError(f"cannot tokenize at: {text[pos:pos + 40]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        out.append((kind, m.group()))
    return out


class Parser:
    """Recursive descent over the emitted subset."""

    def __init__(self, tokens: list[tuple[str, str]], site_counter: list[int]):
        self.toks = tokens
        self.i = 0
        self.site_counter = site_counter

    # ---- helpers
    def peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise PineError("unexpected end of expression")
        self.i += 1
        return tok

    def accept(self, value: str) -> bool:
        tok = self.peek()
        if tok and tok[1] == value:
            self.i += 1
            return True
        return False

    def expect(self, value: str) -> None:
        if not self.accept(value):
            raise PineError(f"expected {value!r}, got {self.peek()}")

    def new_site(self) -> int:
        self.site_counter[0] += 1
        return self.site_counter[0]

    # ---- grammar (lowest precedence first)
    def parse(self) -> Node:
        node = self.ternary()
        if self.peek() is not None:
            raise PineError(f"trailing tokens: {self.toks[self.i:]}")
        return node

    def argument(self) -> Node:
        """Pine allows named arguments: ``title="Long"``. The name is kept so an
        interpreted call can find it, and ignored where the call is ignored."""
        tok = self.peek()
        nxt = self.toks[self.i + 1] if self.i + 1 < len(self.toks) else None
        if tok and tok[0] == "name" and nxt and nxt[1] == "=":
            self.next()
            self.next()
            return Node("kwarg", tok[1], [self.ternary()])
        return self.ternary()

    def ternary(self) -> Node:
        cond = self.or_()
        if self.accept("?"):
            a = self.ternary()
            self.expect(":")
            b = self.ternary()
            return Node("ternary", children=[cond, a, b])
        return cond

    def or_(self) -> Node:
        node = self.and_()
        while self.peek() and self.peek()[1] == "or":
            self.next()
            node = Node("binop", "or", [node, self.and_()])
        return node

    def and_(self) -> Node:
        node = self.not_()
        while self.peek() and self.peek()[1] == "and":
            self.next()
            node = Node("binop", "and", [node, self.not_()])
        return node

    def not_(self) -> Node:
        if self.peek() and self.peek()[1] == "not":
            self.next()
            return Node("unop", "not", [self.not_()])
        return self.comparison()

    def comparison(self) -> Node:
        node = self.additive()
        while self.peek() and self.peek()[1] in ("==", "!=", "<", ">", "<=", ">="):
            op = self.next()[1]
            node = Node("binop", op, [node, self.additive()])
        return node

    def additive(self) -> Node:
        node = self.multiplicative()
        while self.peek() and self.peek()[1] in ("+", "-"):
            op = self.next()[1]
            node = Node("binop", op, [node, self.multiplicative()])
        return node

    def multiplicative(self) -> Node:
        node = self.unary()
        while self.peek() and self.peek()[1] in ("*", "/", "%"):
            op = self.next()[1]
            node = Node("binop", op, [node, self.unary()])
        return node

    def unary(self) -> Node:
        if self.peek() and self.peek()[1] == "-":
            self.next()
            return Node("unop", "neg", [self.unary()])
        return self.postfix()

    def postfix(self) -> Node:
        node = self.primary()
        while self.peek() and self.peek()[1] == "[":
            self.next()
            index = self.ternary()
            self.expect("]")
            node = Node("history", children=[node, index])
        return node

    def primary(self) -> Node:
        kind, text = self.next()
        if text == "(":
            node = self.ternary()
            self.expect(")")
            return node
        if kind == "num":
            return Node("const", float(text))
        if kind == "str":
            return Node("const", text[1:-1])
        if text == "true":
            return Node("const", True)
        if text == "false":
            return Node("const", False)
        if text == "na":
            if self.peek() and self.peek()[1] == "(":
                self.next()
                arg = self.ternary()
                self.expect(")")
                return Node("call", "na", [arg], site=self.new_site())
            return Node("const", NA)
        if kind == "name":
            if self.peek() and self.peek()[1] == "(":
                self.next()
                args: list[Node] = []
                if not self.accept(")"):
                    args.append(self.argument())
                    while self.accept(","):
                        args.append(self.argument())
                    self.expect(")")
                return Node("call", text, args, site=self.new_site())
            return Node("ident", text)
        raise PineError(f"unexpected token {text!r}")


# ============================================================ statement model
@dataclass
class Stmt:
    kind: str  # assign | reassign | var | if | call | augassign
    target: str = ""
    expr: Node | None = None
    body: list["Stmt"] = field(default_factory=list)
    orelse: list["Stmt"] = field(default_factory=list)
    op: str = ""
    raw: str = ""


IGNORED_CALLS = {
    "plot", "plotshape", "plotchar", "plotarrow", "hline", "bgcolor", "fill",
    "alertcondition", "alert", "label.new", "indicator", "strategy", "input",
    "input.int", "input.float", "input.bool", "table.new",
}
STRATEGY_CALLS = {"strategy.entry", "strategy.exit", "strategy.close", "strategy.close_all"}


def parse_program(source: str) -> list[Stmt]:
    lines = source.splitlines()
    site_counter = [0]
    stmts, _ = _parse_block(lines, 0, 0, site_counter)
    return stmts


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_comment(line: str) -> str:
    out, in_str = [], False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            in_str = not in_str
        if not in_str and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _parse_block(lines: list[str], start: int, indent: int, site_counter: list[int]) -> tuple[list[Stmt], int]:
    stmts: list[Stmt] = []
    i = start
    while i < len(lines):
        raw = lines[i]
        code = _strip_comment(raw)
        if not code.strip():
            i += 1
            continue
        cur_indent = _indent(code)
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise PineError(f"unexpected indent at line {i + 1}: {raw!r}")
        body = code.strip()
        i += 1

        if body.startswith("if "):
            cond = Parser(tokenize(body[3:]), site_counter).parse()
            inner, i = _parse_block(lines, i, indent + 4, site_counter)
            stmt = Stmt("if", expr=cond, body=inner, raw=body)
            # optional else
            while i < len(lines) and not _strip_comment(lines[i]).strip():
                i += 1
            if i < len(lines):
                nxt = _strip_comment(lines[i]).strip()
                if nxt == "else" and _indent(_strip_comment(lines[i])) == indent:
                    i += 1
                    stmt.orelse, i = _parse_block(lines, i, indent + 4, site_counter)
            stmts.append(stmt)
            continue

        if body.startswith("var "):
            rest = body[4:].strip()
            parts = rest.split("=", 1)
            decl = parts[0].strip().split()
            name = decl[-1]
            expr = Parser(tokenize(parts[1]), site_counter).parse() if len(parts) > 1 else Node("const", NA)
            stmts.append(Stmt("var", target=name, expr=expr, raw=body))
            continue

        if ":=" in body:
            target, expr_text = body.split(":=", 1)
            stmts.append(
                Stmt("reassign", target=target.strip(), expr=Parser(tokenize(expr_text), site_counter).parse(), raw=body)
            )
            continue

        for aug in ("+=", "-="):
            if aug in body and not body.split(aug)[0].strip().endswith(("<", ">", "!", "=")):
                target, expr_text = body.split(aug, 1)
                stmts.append(
                    Stmt(
                        "augassign",
                        target=target.strip(),
                        op=aug[0],
                        expr=Parser(tokenize(expr_text), site_counter).parse(),
                        raw=body,
                    )
                )
                break
        else:
            head = re.match(r"^([A-Za-z_][A-Za-z_0-9.]*)\s*\(", body)
            if head and head.group(1) in IGNORED_CALLS:
                stmts.append(Stmt("noop", raw=body))
                continue
            m = re.match(r"^([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.+)$", body)
            if m:
                stmts.append(
                    Stmt("assign", target=m.group(1), expr=Parser(tokenize(m.group(2)), site_counter).parse(), raw=body)
                )
            else:
                stmts.append(Stmt("call", expr=Parser(tokenize(body), site_counter).parse(), raw=body))
            continue
    return stmts, i


# =================================================================== runtime
class PineRuntime:
    """Executes the parsed program bar by bar with Pine's series semantics."""

    def __init__(self, bars, mintick: float = 0.25, tz: str | None = None):
        self.bars = bars
        self.mintick = mintick
        self.tz = tz or bars.tz
        self.n = len(bars)
        self.i = 0
        self.vars: dict[str, Any] = {}
        self.history: dict[str, list[Any]] = {}
        self.persistent: set[str] = set()
        self.state: dict[int, Any] = {}
        self.session_cache: dict[tuple[str, str], np.ndarray] = {}
        self.entries: list[dict] = []
        self.position_size = 0.0
        self.position_avg_price = NA

    # -------------------------------------------------------------- program
    def run(self, program: list[Stmt], collect: list[str]) -> dict[str, np.ndarray]:
        out = {name: np.zeros(self.n, dtype=object) for name in collect}
        for i in range(self.n):
            self.i = i
            for name in list(self.vars):
                if name not in self.persistent:
                    del self.vars[name]
            self._exec_block(program)
            for name in collect:
                out[name][i] = self.vars.get(name, NA)
            for name, value in self.vars.items():
                self.history.setdefault(name, []).append(value)
            for name in self.history:
                if name not in self.vars:
                    self.history[name].append(NA)
        return {k: _to_bool_or_float(v) for k, v in out.items()}

    def _exec_block(self, stmts: list[Stmt]) -> None:
        for s in stmts:
            self._exec(s)

    def _exec(self, s: Stmt) -> None:
        if s.kind == "var":
            if s.target not in self.persistent:
                self.persistent.add(s.target)
                self.vars[s.target] = self.eval(s.expr)
            return
        if s.kind == "assign":
            self.vars[s.target] = self.eval(s.expr)
            return
        if s.kind == "reassign":
            self.vars[s.target] = self.eval(s.expr)
            return
        if s.kind == "augassign":
            cur = self.vars.get(s.target, 0.0)
            delta = self.eval(s.expr)
            self.vars[s.target] = (cur + delta) if s.op == "+" else (cur - delta)
            return
        if s.kind == "if":
            if _truthy(self.eval(s.expr)):
                self._exec_block(s.body)
            else:
                self._exec_block(s.orelse)
            return
        if s.kind == "call":
            self.eval(s.expr)
            return
        if s.kind == "noop":
            return
        raise PineError(f"unhandled statement kind {s.kind}")

    # ----------------------------------------------------------- expressions
    def eval(self, node: Node) -> Any:
        kind = node.kind
        if kind == "const":
            return node.value
        if kind == "ident":
            return self._ident(node.value)
        if kind == "history":
            return self._history(node)
        if kind == "unop":
            v = self.eval(node.children[0])
            return (not _truthy(v)) if node.value == "not" else -_num(v)
        if kind == "binop":
            return self._binop(node)
        if kind == "ternary":
            return self.eval(node.children[1]) if _truthy(self.eval(node.children[0])) else self.eval(node.children[2])
        if kind == "call":
            return self._call(node)
        if kind == "kwarg":
            return self.eval(node.children[0])
        raise PineError(f"unhandled node {kind}")

    def _binop(self, node: Node) -> Any:
        op = node.value
        if op == "and":
            return _truthy(self.eval(node.children[0])) and _truthy(self.eval(node.children[1]))
        if op == "or":
            return _truthy(self.eval(node.children[0])) or _truthy(self.eval(node.children[1]))
        a, b = self.eval(node.children[0]), self.eval(node.children[1])
        if op in ("==", "!="):
            eq = (a == b) if not (_isna(a) or _isna(b)) else (_isna(a) and _isna(b))
            return eq if op == "==" else not eq
        if _isna(a) or _isna(b):
            return NA if op in "+-*/%" else False
        a, b = _num(a), _num(b)
        return {
            "+": a + b, "-": a - b, "*": a * b,
            "/": (a / b if b else NA), "%": (a % b if b else NA),
            "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b,
        }[op]

    def _ident(self, name: str) -> Any:
        i = self.i
        bars = self.bars
        builtin = {
            "close": lambda: bars.close[i],
            "open": lambda: bars.open[i],
            "high": lambda: bars.high[i],
            "low": lambda: bars.low[i],
            "volume": lambda: bars.volume[i],
            "hl2": lambda: (bars.high[i] + bars.low[i]) / 2,
            "hlc3": lambda: (bars.high[i] + bars.low[i] + bars.close[i]) / 3,
            "ohlc4": lambda: (bars.open[i] + bars.high[i] + bars.low[i] + bars.close[i]) / 4,
            "time": lambda: float(bars.df["ts"].iloc[i].value // 10**6),
            "bar_index": lambda: float(i),
            "dayofweek": lambda: float(bars.weekday[i] + 2 if bars.weekday[i] < 6 else 1),
            "syminfo.mintick": lambda: self.mintick,
            "timeframe.period": lambda: bars.timeframe,
            "barstate.isconfirmed": lambda: True,  # backtest: every historical bar is closed
            "barstate.islast": lambda: i == self.n - 1,
            "session.islastbar": lambda: (
                i == self.n - 1 or bars.local_date[i] != bars.local_date[min(i + 1, self.n - 1)]
            ),
            "strategy.position_size": lambda: self.position_size,
            "strategy.position_avg_price": lambda: self.position_avg_price,
            "strategy.long": lambda: "long",
            "strategy.short": lambda: "short",
            "na": lambda: NA,
        }
        if name in builtin:
            return builtin[name]()
        if name in self.vars:
            return self.vars[name]
        if name.startswith(("color.", "shape.", "location.", "size.", "plot.", "alert.", "strategy.")):
            return name
        raise PineError(f"unknown identifier {name!r} at bar {i}")

    def _history(self, node: Node) -> Any:
        base, index_node = node.children
        offset = int(_num(self.eval(index_node)))
        if base.kind == "ident":
            name = base.value
            if name in ("close", "open", "high", "low", "volume"):
                j = self.i - offset
                return getattr(self.bars, name)[j] if j >= 0 else NA
            series = self.history.get(name, [])
            j = len(series) - offset
            return series[j] if 0 <= j < len(series) else NA
        # history of an expression: keep a per-site ring buffer
        key = ("hist", node.site if node.site >= 0 else id(node))
        buf = self.state.setdefault(key, [])
        value = self.eval(base)
        if len(buf) <= self.i:
            buf.append(value)
        j = self.i - offset
        return buf[j] if 0 <= j < len(buf) else NA

    # -------------------------------------------------------------- builtins
    def _call(self, node: Node) -> Any:
        name = node.value
        if name in IGNORED_CALLS:
            return NA
        if name in STRATEGY_CALLS:
            return self._strategy_call(name, node)
        args = node.children
        i = self.i

        if name == "na":
            return _isna(self.eval(args[0]))
        if name == "nz":
            v = self.eval(args[0])
            return (self.eval(args[1]) if len(args) > 1 else 0.0) if _isna(v) else v
        if name in ("math.max", "math.min"):
            values = [_num(self.eval(a)) for a in args]
            if any(math.isnan(v) for v in values):
                return NA
            return max(values) if name.endswith("max") else min(values)
        if name == "math.abs":
            return abs(_num(self.eval(args[0])))
        if name == "math.round":
            return float(round(_num(self.eval(args[0]))))
        if name == "str.tostring":
            return str(self.eval(args[0]))
        if name == "time":
            if len(args) >= 2:
                sess = self.eval(args[1])
                tz = self.eval(args[2]) if len(args) > 2 else self.tz
                return self._session_time(str(sess), str(tz))
            if len(args) == 1:
                # time("D") is the OPENING time of the containing period, not the
                # bar's own timestamp. Getting this wrong makes ta.change(time("D"))
                # fire on every bar, which silently resets every `var` in the script.
                return self._period_open(str(self.eval(args[0])))
            return float(self.bars.df["ts"].iloc[i].value // 10**6)
        if name == "ta.change":
            return self._ta_change(node)
        if name == "ta.cum":
            acc = self.state.get(("cum", node.site), 0.0)
            acc = acc + _num0(self.eval(args[0]))
            self.state[("cum", node.site)] = acc
            return acc
        if name in ("ta.ema", "ta.sma", "ta.rma", "ta.stdev", "ta.atr", "ta.rsi", "ta.vwap", "ta.highest", "ta.lowest"):
            return self._ta_series(name, node)
        if name in ("ta.crossover", "ta.crossunder"):
            return self._ta_cross(name, node)
        raise PineError(f"unimplemented Pine function {name!r}")

    def _strategy_call(self, name: str, node: Node) -> Any:
        if name == "strategy.entry":
            positional = [c for c in node.children if c.kind != "kwarg"]
            side = self.eval(positional[1]) if len(positional) > 1 else "long"
            self.entries.append({"bar": self.i, "side": "long" if side == "long" else "short"})
            self.position_size = 1.0 if side == "long" else -1.0
            self.position_avg_price = self.bars.close[self.i]
        elif name in ("strategy.close", "strategy.close_all"):
            self.position_size = 0.0
            self.position_avg_price = NA
        return NA

    # ---- stateful series helpers
    def _series_buffer(self, key, node: Node) -> list[float]:
        buf = self.state.setdefault(key, [])
        value = _num0(self.eval(node))
        if len(buf) <= self.i:
            buf.append(value)
        else:
            buf[self.i] = value
        return buf

    def _ta_change(self, node: Node) -> Any:
        buf = self._series_buffer(("chg", node.site), node.children[0])
        if len(buf) < 2:
            return NA
        return buf[-1] - buf[-2]

    def _ta_cross(self, name: str, node: Node) -> bool:
        a = self._series_buffer(("xa", node.site), node.children[0])
        b = self._series_buffer(("xb", node.site), node.children[1])
        if len(a) < 2 or len(b) < 2:
            return False
        if any(math.isnan(x) for x in (a[-1], a[-2], b[-1], b[-2])):
            return False
        if name == "ta.crossover":
            return a[-2] <= b[-2] and a[-1] > b[-1]
        return a[-2] >= b[-2] and a[-1] < b[-1]

    def _ta_series(self, name: str, node: Node) -> Any:
        args = node.children
        if name == "ta.atr":
            length = int(_num(self.eval(args[0])))
            source_buf = self._true_range_buffer()
            return _rma_step(self.state, ("atr", node.site), source_buf, length, self.i)
        if name == "ta.vwap":
            return self._vwap(node)
        if name == "ta.rsi":
            length = int(_num(self.eval(args[1])))
            src = self._series_buffer(("rsisrc", node.site), args[0])
            return _rsi_step(self.state, ("rsi", node.site), src, length, self.i)
        src = self._series_buffer(("src", node.site), args[0])
        length = int(_num(self.eval(args[1])))
        if name == "ta.sma":
            return _sma_step(src, length, self.i)
        if name == "ta.ema":
            return _ema_step(self.state, ("ema", node.site), src, length, self.i)
        if name == "ta.rma":
            return _rma_step(self.state, ("rma", node.site), src, length, self.i)
        if name == "ta.stdev":
            return _stdev_step(src, length, self.i)
        if name == "ta.highest":
            window = src[max(0, self.i - length + 1) : self.i + 1]
            return max(window) if len(window) == length else NA
        if name == "ta.lowest":
            window = src[max(0, self.i - length + 1) : self.i + 1]
            return min(window) if len(window) == length else NA
        raise PineError(f"unimplemented series function {name}")

    def _true_range_buffer(self) -> list[float]:
        buf = self.state.setdefault(("tr",), [])
        if len(buf) <= self.i:
            i = self.i
            prev_close = self.bars.close[i - 1] if i > 0 else self.bars.close[0]
            buf.append(
                max(
                    self.bars.high[i] - self.bars.low[i],
                    abs(self.bars.high[i] - prev_close),
                    abs(self.bars.low[i] - prev_close),
                )
            )
        return buf

    def _vwap(self, node: Node) -> Any:
        key = ("vwap", node.site)
        state = self.state.setdefault(key, {"day": None, "pv": 0.0, "vv": 0.0, "last": -1})
        if state["last"] == self.i:
            return state["value"]
        day = self.bars.local_date[self.i]
        if day != state["day"]:
            state.update({"day": day, "pv": 0.0, "vv": 0.0})
        src = _num0(self.eval(node.children[0]))
        state["pv"] += src * self.bars.volume[self.i]
        state["vv"] += self.bars.volume[self.i]
        state["value"] = state["pv"] / state["vv"] if state["vv"] > 0 else NA
        state["last"] = self.i
        return state["value"]

    def _period_open(self, resolution: str) -> float:
        """Opening timestamp of the period containing this bar, in exchange local
        time. Only the resolutions the emitter uses are implemented."""
        key = ("period_open", resolution)
        if key not in self.session_cache:
            local = self.bars.df["ts"].dt.tz_convert(self.tz)
            res = resolution.strip().upper()
            if res in ("D", "1D"):
                stamps = local.dt.floor("D")
            elif res in ("W", "1W"):
                stamps = local.dt.to_period("W").dt.start_time
            elif res in ("M", "1M"):
                stamps = local.dt.to_period("M").dt.start_time
            else:
                raise PineError(f"time({resolution!r}) is not implemented")
            self.session_cache[key] = np.asarray(
                [float(pd_ts.value // 10**6) for pd_ts in stamps], dtype=float
            )
        return float(self.session_cache[key][self.i])

    def _session_time(self, session: str, tz: str) -> Any:
        """``time(timeframe.period, "0830-0930:1234567", tz)`` -> timestamp or na."""
        key = (session, tz)
        if key not in self.session_cache:
            window, _, days = session.partition(":")
            start_s, _, end_s = window.partition("-")
            start = int(start_s[:2]) * 60 + int(start_s[2:])
            end = int(end_s[:2]) * 60 + int(end_s[2:])
            allowed = {int(d) for d in (days or "1234567")}
            local = self.bars.df["ts"].dt.tz_convert(tz)
            minutes = (local.dt.hour * 60 + local.dt.minute).to_numpy()
            # Pine's dayofweek: Sunday = 1 .. Saturday = 7
            weekday = ((local.dt.weekday.to_numpy() + 1) % 7) + 1
            inside = (minutes >= start) & (minutes < end) if start <= end else (
                (minutes >= start) | (minutes < end)
            )
            self.session_cache[key] = inside & np.isin(weekday, list(allowed))
        return float(self.bars.df["ts"].iloc[self.i].value // 10**6) if self.session_cache[key][self.i] else NA


# ------------------------------------------------------------------- numerics
def _sma_step(buf: list[float], length: int, i: int) -> float:
    if i + 1 < length:
        return NA
    window = buf[i - length + 1 : i + 1]
    if any(math.isnan(x) for x in window):
        return NA
    return sum(window) / length


def _stdev_step(buf: list[float], length: int, i: int) -> float:
    if i + 1 < length:
        return NA
    window = buf[i - length + 1 : i + 1]
    if any(math.isnan(x) for x in window):
        return NA
    return float(np.std(window))


def _ema_step(state: dict, key, buf: list[float], length: int, i: int) -> float:
    st = state.setdefault(key, {"value": NA, "i": -1})
    if st["i"] == i:
        return st["value"]
    if i + 1 < length:
        st.update({"i": i, "value": NA})
        return NA
    alpha = 2.0 / (length + 1.0)
    if math.isnan(st["value"]):
        seed = buf[i - length + 1 : i + 1]
        value = sum(seed) / length
    else:
        value = alpha * buf[i] + (1 - alpha) * st["value"]
    st.update({"i": i, "value": value})
    return value


def _rma_step(state: dict, key, buf: list[float], length: int, i: int) -> float:
    st = state.setdefault(key, {"value": NA, "i": -1})
    if st["i"] == i:
        return st["value"]
    if i + 1 < length:
        st.update({"i": i, "value": NA})
        return NA
    if math.isnan(st["value"]):
        value = sum(buf[i - length + 1 : i + 1]) / length
    else:
        value = (st["value"] * (length - 1) + buf[i]) / length
    st.update({"i": i, "value": value})
    return value


def _rsi_step(state: dict, key, buf: list[float], length: int, i: int) -> float:
    st = state.setdefault(key, {"gain": NA, "loss": NA, "i": -1})
    if st["i"] == i:
        return st["value"]
    if i == 0:
        st.update({"i": i, "value": NA})
        return NA
    change = buf[i] - buf[i - 1]
    gain, loss = max(change, 0.0), max(-change, 0.0)
    gains = state.setdefault((key, "g"), [])
    losses = state.setdefault((key, "l"), [])
    if len(gains) <= i:
        gains.append(gain)
        losses.append(loss)
    g = _rma_step(state, (key, "rg"), gains, length, i)
    l = _rma_step(state, (key, "rl"), losses, length, i)
    if math.isnan(g) or math.isnan(l):
        value = NA
    elif l == 0:
        value = 100.0
    else:
        value = 100.0 - (100.0 / (1.0 + g / l))
    st.update({"i": i, "value": value})
    return value


def _isna(v: Any) -> bool:
    return isinstance(v, float) and math.isnan(v)


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if _isna(v) or v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    return bool(v)


def _num(v: Any) -> float:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if v is None:
        return NA
    if isinstance(v, str):
        raise PineError(f"cannot use string {v!r} as a number")
    return float(v)


def _num0(v: Any) -> float:
    try:
        return _num(v)
    except PineError:
        return NA


def _to_bool_or_float(arr: np.ndarray) -> np.ndarray:
    if all(isinstance(x, (bool, np.bool_)) for x in arr):
        return arr.astype(bool)
    return np.array([_num0(x) for x in arr], dtype=float)


def run_pine(source: str, bars, collect: list[str], mintick: float = 0.25) -> dict[str, np.ndarray]:
    """Parse and execute emitted Pine over ``bars``, returning the named series."""
    program = parse_program(source)
    rt = PineRuntime(bars, mintick=mintick)
    return rt.run(program, collect)
