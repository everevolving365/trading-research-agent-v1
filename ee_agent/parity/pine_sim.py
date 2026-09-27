"""A Pine interpreter for the scripts this project emits -- and the owner's own.

Why this exists
---------------
Law Two says the Python backtest, the Pine indicator, the Pine strategy and the
live config are the same object, provably. Re-running the Python plan and
calling that "parity" proves nothing -- it compares a thing to itself.

So the parity harness parses and executes the **emitted Pine source text**, bar
by bar, with Pine's own series semantics, and compares the resulting signal
series against the Python engine's. A divergence is located to the bar.

It also runs the **owner's original TradingView script** unmodified (the fifth
parity target for a library strategy): if the generated strategy and the script
the owner actually trades from disagree on a single bar, that is reported.

What it does and does not prove
-------------------------------
It proves the scripts compute the same signals as the Python engine on the same
bars, under a faithful implementation of the Pine constructs used. It does not
prove TradingView's servers agree -- only TradingView can do that, which is
what ability 46 (the Operator's deep backtest cross-check) is for. The Python
engine remains authoritative (Section 9, honest limit 1).

Supported: v5 and v6 scripts using expressions, ``var``, ``if/else if/else``,
``for ... to ... [by]`` with ``break``/``continue``, user functions (``=>``,
local scope, per-call-site state for built-ins), tuples, arrays, ``input.*``
(default values, or overrides by title), ``request.security`` on intraday
higher timeframes (``lookahead_off``), ``ta.*`` as used, and drawing calls
(executed as no-ops). Anything else fails loudly: the interpreter never skips a
construct it does not recognise.

Two semantics that matter and are easy to get wrong:

* ``and``/``or`` are lazy in v6 and evaluate both sides in v5.
* ``request.security_lower_tf`` has no lower-timeframe data here; it returns a
  one-element array holding the chart bar's own value. That is exact for
  anything read at a chart-bar boundary (an open at 08:30 on a 5-minute chart)
  and is recorded in :attr:`PineRuntime.notes` whenever it is used.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

NA = float("nan")


class PineError(Exception):
    """The interpreter met something it does not implement. Never silent."""


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


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
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<color>\#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?)
  | (?P<num>\d+\.\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?|\d+(?:[eE][-+]?\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)
  | (?P<op>:=|==|!=|<=|>=|=>|\+=|-=|\*=|/=|\?|:|\(|\)|\[|\]|,|\+|-|\*|/|%|<|>|=)
    """,
    re.VERBOSE,
)

KEYWORDS = {"and", "or", "not", "var", "varip", "if", "else", "true", "false", "na", "for", "while"}
_GENERIC_RE = re.compile(r"\b((?:array|matrix|map)\.new(?:_\w+)?)\s*<[^()=]*?>(?=\s*\()")
_TYPE_WORDS = {"int", "float", "bool", "string", "color", "line", "label", "box", "table", "linefill", "polyline"}


def _strip_generics(text: str) -> str:
    return _GENERIC_RE.sub(r"\1", text)


def _unescape(text: str) -> str:
    return text.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")


def tokenize(text: str) -> list[tuple[str, str]]:
    text = _strip_generics(text)
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
    """Recursive descent over expressions."""

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
        if tok and tok[1] == value and tok[0] != "str":
            self.i += 1
            return True
        return False

    def expect(self, value: str) -> None:
        if not self.accept(value):
            raise PineError(f"expected {value!r}, got {self.peek()}")

    def new_site(self) -> int:
        self.site_counter[0] += 1
        return self.site_counter[0]

    def _is(self, value: str) -> bool:
        tok = self.peek()
        return bool(tok) and tok[1] == value and tok[0] != "str"

    # ---- grammar (lowest precedence first)
    def parse(self) -> Node:
        node = self.ternary()
        if self.peek() is not None:
            raise PineError(f"trailing tokens: {self.toks[self.i:]}")
        return node

    def argument(self) -> Node:
        """Pine allows named arguments: ``title="Long"``."""
        tok = self.peek()
        nxt = self.toks[self.i + 1] if self.i + 1 < len(self.toks) else None
        if tok and tok[0] == "name" and nxt and nxt[1] == "=" and nxt[0] == "op":
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
        while self._is("or"):
            self.next()
            node = Node("binop", "or", [node, self.and_()])
        return node

    def and_(self) -> Node:
        node = self.not_()
        while self._is("and"):
            self.next()
            node = Node("binop", "and", [node, self.not_()])
        return node

    def not_(self) -> Node:
        if self._is("not"):
            self.next()
            return Node("unop", "not", [self.not_()])
        return self.comparison()

    def comparison(self) -> Node:
        node = self.additive()
        while self.peek() and self.peek()[0] == "op" and self.peek()[1] in ("==", "!=", "<", ">", "<=", ">="):
            op = self.next()[1]
            node = Node("binop", op, [node, self.additive()])
        return node

    def additive(self) -> Node:
        node = self.multiplicative()
        while self.peek() and self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.next()[1]
            node = Node("binop", op, [node, self.multiplicative()])
        return node

    def multiplicative(self) -> Node:
        node = self.unary()
        while self.peek() and self.peek()[0] == "op" and self.peek()[1] in ("*", "/", "%"):
            op = self.next()[1]
            node = Node("binop", op, [node, self.unary()])
        return node

    def unary(self) -> Node:
        if self._is("-"):
            self.next()
            return Node("unop", "neg", [self.unary()])
        if self._is("+"):
            self.next()
            return self.unary()
        return self.postfix()

    def postfix(self) -> Node:
        node = self.primary()
        while self._is("["):
            self.next()
            index = self.ternary()
            self.expect("]")
            node = Node("history", children=[node, index], site=self.new_site())
        return node

    def primary(self) -> Node:
        kind, text = self.next()
        if kind == "str":
            return Node("const", _unescape(text[1:-1]))
        if kind == "color":
            return Node("const", text)
        if kind == "num":
            return Node("const", float(text))
        if text == "(":
            node = self.ternary()
            self.expect(")")
            return node
        if text == "[":
            items: list[Node] = []
            if not self.accept("]"):
                items.append(self.ternary())
                while self.accept(","):
                    items.append(self.ternary())
                self.expect("]")
            return Node("tuple", children=items)
        if text == "true":
            return Node("const", True)
        if text == "false":
            return Node("const", False)
        if text == "na":
            if self._is("("):
                self.next()
                arg = self.ternary()
                self.expect(")")
                return Node("call", "na", [arg], site=self.new_site())
            return Node("const", NA)
        if kind == "name":
            if self._is("("):
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
    kind: str  # assign|reassign|var|if|for|call|augassign|tuple|multi|func|break|continue|noop
    target: Any = ""
    expr: Node | None = None
    body: list["Stmt"] = field(default_factory=list)
    orelse: list["Stmt"] = field(default_factory=list)
    op: str = ""
    raw: str = ""
    extra: dict = field(default_factory=dict)


#: Calls that only draw, declare or alert. They return na and their arguments
#: are never evaluated -- they cannot influence a signal.
NOOP_CALLS = {
    "plot", "plotshape", "plotchar", "plotarrow", "plotcandle", "plotbar", "hline", "bgcolor",
    "barcolor", "fill", "alertcondition", "alert", "indicator", "strategy", "library",
    "log.info", "log.warning", "log.error",
}
NOOP_PREFIXES = (
    "box.set_", "box.delete", "line.set_", "line.delete", "label.set_", "label.delete",
    "table.cell", "table.set_", "table.delete", "table.clear", "table.merge_cells",
    "linefill.", "polyline.delete",
)
DRAWING_NEW = {"box.new", "line.new", "label.new", "table.new", "polyline.new", "linefill.new"}
STRATEGY_CALLS = {"strategy.entry", "strategy.exit", "strategy.close", "strategy.close_all"}
#: Kept for callers that imported the old name.
IGNORED_CALLS = NOOP_CALLS | {"input", "input.int", "input.float", "input.bool", "table.new", "label.new"}

ENUM_PREFIXES = (
    "color.", "shape.", "location.", "size.", "plot.", "alert.", "strategy.", "barmerge.", "format.",
    "text.", "label.", "line.", "extend.", "xloc.", "yloc.", "display.", "position.", "font.",
    "order.", "scale.", "hline.", "currency.", "dayofweek.", "session.", "adjustment.", "backadjustment.",
)

_FUNC_RE = re.compile(r"^([A-Za-z_]\w*)\s*\(([^()]*)\)\s*=>\s*(.*)$")
_FOR_RE = re.compile(r"^for\s+([A-Za-z_]\w*)\s*=\s*(.+?)\s+to\s+(.+?)(?:\s+by\s+(.+))?$")
_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")


def _scan_top(body: str):
    """Yield (index, char) for characters at bracket depth 0 outside strings."""
    depth = 0
    quote = None
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0:
            yield i, ch
        i += 1


def _find_assign(body: str) -> tuple[int, str] | None:
    """Position and operator of the top-level assignment, if there is one."""
    for i, ch in _scan_top(body):
        two = body[i:i + 2]
        if two in (":=", "+=", "-=", "*=", "/="):
            return i, two
        if ch == "=":
            prev = body[i - 1] if i > 0 else ""
            nxt = body[i + 1] if i + 1 < len(body) else ""
            if nxt in ("=", ">") or prev in ("=", "!", "<", ">", ":", "+", "-", "*", "/"):
                continue
            return i, "="
    return None


def _split_top(body: str, sep: str = ",") -> list[str]:
    parts, last = [], 0
    for i, ch in _scan_top(body):
        if ch == sep:
            parts.append(body[last:i])
            last = i + 1
    parts.append(body[last:])
    return [p.strip() for p in parts]


def _strip_comment(line: str) -> str:
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
            return line[:i].rstrip()
        i += 1
    return line.rstrip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _logical_lines(source: str) -> list[tuple[int, str]]:
    """(line number, code) with comments stripped and continuation lines joined.
    Pine continues a statement on a line indented by a non-multiple of four."""
    out: list[tuple[int, str]] = []
    for no, raw in enumerate(source.replace("\t", "    ").splitlines(), start=1):
        code = _strip_comment(raw)
        if not code.strip():
            continue
        ind = _indent(code)
        if out and ind % 4 != 0:
            prev_no, prev = out[-1]
            out[-1] = (prev_no, prev + " " + code.strip())
            continue
        out.append((no, code))
    return out


def parse_program(source: str) -> list[Stmt]:
    lines = _logical_lines(source)
    site_counter = [0]
    stmts, i = _parse_block(lines, 0, 0, site_counter)
    if i < len(lines):
        no, code = lines[i]
        raise PineError(f"unexpected indent at line {no}: {code!r}")
    return stmts


def _expr(text: str, sites: list[int]) -> Node:
    return Parser(tokenize(text), sites).parse()


def _parse_block(lines, start: int, indent: int, sites: list[int]) -> tuple[list[Stmt], int]:
    stmts: list[Stmt] = []
    i = start
    while i < len(lines):
        no, code = lines[i]
        cur = _indent(code)
        if cur < indent:
            break
        if cur > indent:
            raise PineError(f"unexpected indent at line {no}: {code!r}")
        body = code.strip()
        i += 1

        if body.startswith("if ") or body.startswith("if("):
            stmt, i = _parse_if(lines, i, indent, body[2:].strip(), sites, body)
            stmts.append(stmt)
            continue

        m = _FOR_RE.match(body)
        if m:
            inner, i = _parse_block(lines, i, indent + 4, sites)
            stmts.append(
                Stmt(
                    "for",
                    target=m.group(1),
                    body=inner,
                    raw=body,
                    extra={
                        "start": _expr(m.group(2), sites),
                        "end": _expr(m.group(3), sites),
                        "step": _expr(m.group(4), sites) if m.group(4) else None,
                    },
                )
            )
            continue
        if body.startswith(("for ", "while ", "switch", "type ", "method ", "export ", "import ")):
            raise PineError(f"line {no}: construct not implemented: {body!r}")

        if body == "break":
            stmts.append(Stmt("break", raw=body))
            continue
        if body == "continue":
            stmts.append(Stmt("continue", raw=body))
            continue

        m = _FUNC_RE.match(body)
        if m:
            params = []
            for p in _split_top(m.group(2)) if m.group(2).strip() else []:
                if "=" in p:
                    pname, default = p.split("=", 1)
                    params.append((pname.strip().split()[-1], _expr(default, sites)))
                else:
                    params.append((p.split()[-1], None))
            inline = m.group(3).strip()
            if inline:
                stmts.append(Stmt("func", target=m.group(1), expr=_expr(inline, sites), raw=body,
                                  extra={"params": params, "inline": True}))
            else:
                inner, i = _parse_block(lines, i, indent + 4, sites)
                stmts.append(Stmt("func", target=m.group(1), body=inner, raw=body,
                                  extra={"params": params, "inline": False}))
            continue

        stmts.append(_parse_simple(body, sites, no))
    return stmts, i


def _parse_if(lines, i: int, indent: int, cond_text: str, sites, raw: str) -> tuple[Stmt, int]:
    cond = _expr(cond_text, sites)
    inner, i = _parse_block(lines, i, indent + 4, sites)
    stmt = Stmt("if", expr=cond, body=inner, raw=raw)
    if i < len(lines) and _indent(lines[i][1]) == indent:
        nxt = lines[i][1].strip()
        if nxt == "else":
            stmt.orelse, i = _parse_block(lines, i + 1, indent + 4, sites)
        elif nxt.startswith("else if ") or nxt.startswith("else if("):
            nested, i = _parse_if(lines, i + 1, indent, nxt[len("else if"):].strip(), sites, nxt)
            stmt.orelse = [nested]
    return stmt, i


def _decl_name(lhs: str, no: int) -> str:
    words = _strip_generics(lhs).replace("<", " <").split()
    words = [w for w in words if not w.startswith("<")]
    if not words:
        raise PineError(f"line {no}: empty assignment target")
    name = words[-1]
    if not _IDENT_RE.match(name):
        raise PineError(f"line {no}: cannot assign to {lhs!r}")
    for w in words[:-1]:
        base = re.sub(r"<.*", "", w)
        if base not in _TYPE_WORDS and base not in ("array", "matrix", "map", "series", "simple", "const"):
            raise PineError(f"line {no}: unexpected {w!r} before {name!r}")
    return name


def _parse_simple(body: str, sites: list[int], no: int) -> Stmt:
    is_var = False
    text = body
    for kw in ("var ", "varip "):
        if text.startswith(kw):
            is_var = True
            text = text[len(kw):].strip()
            break

    if not is_var:
        pieces = _split_top(text, ",")
        if len(pieces) > 1 and all((_find_assign(p) or (0, ""))[1] == "=" for p in pieces):
            return Stmt("multi", body=[_parse_simple(p, sites, no) for p in pieces], raw=body)

    found = _find_assign(text)
    if found is None:
        if is_var:
            return Stmt("var", target=_decl_name(text, no), expr=Node("const", NA), raw=body)
        head = re.match(r"^([A-Za-z_][A-Za-z_0-9.]*)\s*\(", text)
        if head and (head.group(1) in NOOP_CALLS or head.group(1).startswith(NOOP_PREFIXES)):
            return Stmt("noop", raw=body)
        return Stmt("call", expr=_expr(text, sites), raw=body)

    pos, op = found
    lhs, rhs = text[:pos].strip(), text[pos + len(op):].strip()
    if lhs.startswith("["):
        if not lhs.endswith("]"):
            raise PineError(f"line {no}: malformed tuple target {lhs!r}")
        names = [n.strip() for n in lhs[1:-1].split(",")]
        return Stmt("tuple", target=names, expr=_expr(rhs, sites), op=op, raw=body)
    expr = _expr(rhs, sites)
    if is_var:
        return Stmt("var", target=_decl_name(lhs, no), expr=expr, raw=body)
    if op == "=":
        return Stmt("assign", target=_decl_name(lhs, no), expr=expr, raw=body)
    if op == ":=":
        return Stmt("reassign", target=_decl_name(lhs, no), expr=expr, raw=body)
    return Stmt("augassign", target=_decl_name(lhs, no), op=op[0], expr=expr, raw=body)


# =================================================================== runtime
def _version(source: str) -> int:
    m = re.search(r"//@version=(\d+)", source)
    return int(m.group(1)) if m else 5


_ZONES: dict[str, Any] = {}


def _zone(tz: str):
    from zoneinfo import ZoneInfo

    z = _ZONES.get(tz)
    if z is None:
        z = _ZONES[tz] = ZoneInfo(tz)
    return z


class PineRuntime:
    """Executes the parsed program bar by bar with Pine's series semantics."""

    def __init__(self, bars, mintick: float = 0.25, tz: str | None = None, version: int = 5,
                 inputs: dict | None = None, functions: dict | None = None):
        from ee_agent.data.bars import _epoch_ns

        self.bars = bars
        self.mintick = mintick
        self.tz = tz or bars.tz
        self.version = version
        self.lazy = version >= 6
        self.inputs = dict(inputs or {})
        self.n = len(bars)
        self.i = 0
        self.vars: dict[str, Any] = {}
        self.frame: dict[str, Any] | None = None
        self.scope: tuple = ()
        self.functions: dict[str, Stmt] = functions if functions is not None else {}
        self.history: dict[str, list[Any]] = {}
        self.persistent: set[str] = set()
        self.state: dict[Any, Any] = {}
        self.session_cache: dict[Any, np.ndarray] = {}
        self.entries: list[dict] = []
        self.position_size = 0.0
        self.position_avg_price = NA
        self.notes: list[str] = []
        self._next_id = 0
        self.time_ms = (_epoch_ns(bars.df["ts"]) // 10**6).astype(float) if self.n else np.zeros(0)

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
            for name, series in self.history.items():
                if len(series) <= i:
                    series.append(NA)
        return {k: _to_bool_or_float(v) for k, v in out.items()}

    def _exec_block(self, stmts: list[Stmt]) -> Any:
        value: Any = NA
        for s in stmts:
            value = self._exec(s)
        return value

    def _exec(self, s: Stmt) -> Any:
        kind = s.kind
        if kind == "assign":
            if s.target in self.inputs and s.expr.kind == "call" and str(s.expr.value).startswith("input"):
                value = self.inputs[s.target]
            else:
                value = self.eval(s.expr)
            self._declare(s.target, value)
            return value
        if kind == "reassign":
            value = self.eval(s.expr)
            self._set(s.target, value)
            return value
        if kind == "if":
            if _truthy(self.eval(s.expr)):
                return self._exec_block(s.body)
            return self._exec_block(s.orelse) if s.orelse else NA
        if kind == "call":
            return self.eval(s.expr)
        if kind == "var":
            if self.frame is not None:
                raise PineError("'var' inside a function is not implemented")
            if s.target not in self.persistent:
                self.persistent.add(s.target)
                self.vars[s.target] = self.eval(s.expr)
            return self.vars[s.target]
        if kind == "for":
            return self._exec_for(s)
        if kind == "augassign":
            cur = self._lookup(s.target)
            delta = self.eval(s.expr)
            if s.op == "+" and (isinstance(cur, str) or isinstance(delta, str)):
                value = str(cur) + str(delta)
            elif _isna(cur) or _isna(delta):
                value = NA
            else:
                a, b = _num(cur), _num(delta)
                value = {"+": a + b, "-": a - b, "*": a * b, "/": (a / b if b else NA)}[s.op]
            self._set(s.target, value)
            return value
        if kind == "tuple":
            values = self.eval(s.expr)
            if not isinstance(values, tuple) or len(values) != len(s.target):
                raise PineError(f"tuple assignment expects {len(s.target)} values: {s.raw!r}")
            for name, value in zip(s.target, values):
                if name == "_":
                    continue
                (self._set if s.op == ":=" else self._declare)(name, value)
            return NA
        if kind == "multi":
            return self._exec_block(s.body)
        if kind == "break":
            raise _Break()
        if kind == "continue":
            raise _Continue()
        if kind == "func":
            self.functions[s.target] = s
            return NA
        if kind == "noop":
            return NA
        raise PineError(f"unhandled statement kind {kind}")

    def _exec_for(self, s: Stmt) -> Any:
        start = self.eval(s.extra["start"])
        end = self.eval(s.extra["end"])
        if _isna(start) or _isna(end):
            return NA
        start, end = _num(start), _num(end)
        step = abs(_num(self.eval(s.extra["step"]))) if s.extra["step"] is not None else 1.0
        if step == 0:
            raise PineError("for-loop step of zero")
        direction = 1.0 if end >= start else -1.0
        v = start
        value: Any = NA
        guard = 0
        while (v <= end) if direction > 0 else (v >= end):
            self._declare(s.target, float(v))
            try:
                value = self._exec_block(s.body)
            except _Break:
                break
            except _Continue:
                pass
            v += step * direction
            guard += 1
            if guard > 1_000_000:
                raise PineError("for-loop ran more than 1,000,000 iterations in one bar")
        return value

    # ------------------------------------------------------------- scoping
    def _declare(self, name: str, value: Any) -> None:
        if self.frame is not None:
            self.frame[name] = value
        else:
            self.vars[name] = value

    def _set(self, name: str, value: Any) -> None:
        frame = self.frame
        if frame is not None and name in frame:
            frame[name] = value
        elif name in self.vars:
            self.vars[name] = value
        elif frame is not None:
            frame[name] = value
        else:
            self.vars[name] = value

    def _lookup(self, name: str) -> Any:
        frame = self.frame
        if frame is not None and name in frame:
            return frame[name]
        if name in self.vars:
            return self.vars[name]
        fn = _BUILTIN_VARS.get(name)
        if fn is not None:
            return fn(self)
        if name.startswith(ENUM_PREFIXES):
            return name
        raise PineError(f"unknown identifier {name!r} at bar {self.i}")

    # ----------------------------------------------------------- expressions
    def eval(self, node: Node) -> Any:
        kind = node.kind
        if kind == "const":
            return node.value
        if kind == "ident":
            return self._lookup(node.value)
        if kind == "binop":
            return self._binop(node)
        if kind == "call":
            return self._call(node)
        if kind == "unop":
            v = self.eval(node.children[0])
            if node.value == "not":
                return not _truthy(v)
            return NA if _isna(v) else -_num(v)
        if kind == "ternary":
            return self.eval(node.children[1]) if _truthy(self.eval(node.children[0])) else self.eval(node.children[2])
        if kind == "history":
            return self._history(node)
        if kind == "tuple":
            return tuple(self.eval(c) for c in node.children)
        if kind == "kwarg":
            return self.eval(node.children[0])
        raise PineError(f"unhandled node {kind}")

    def _binop(self, node: Node) -> Any:
        op = node.value
        if op == "and":
            if self.lazy:
                return _truthy(self.eval(node.children[0])) and _truthy(self.eval(node.children[1]))
            a, b = _truthy(self.eval(node.children[0])), _truthy(self.eval(node.children[1]))
            return a and b
        if op == "or":
            if self.lazy:
                return _truthy(self.eval(node.children[0])) or _truthy(self.eval(node.children[1]))
            a, b = _truthy(self.eval(node.children[0])), _truthy(self.eval(node.children[1]))
            return a or b
        a, b = self.eval(node.children[0]), self.eval(node.children[1])
        if op in ("==", "!="):
            eq = (a == b) if not (_isna(a) or _isna(b)) else (_isna(a) and _isna(b))
            return eq if op == "==" else not eq
        if op == "+" and (isinstance(a, str) or isinstance(b, str)):
            return str(a) + str(b)
        if _isna(a) or _isna(b):
            return NA if op in "+-*/%" else False
        a, b = _num(a), _num(b)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a / b if b else NA
        if op == "%":
            return math.fmod(a, b) if b else NA
        if op == "<":
            return a < b
        if op == ">":
            return a > b
        if op == "<=":
            return a <= b
        if op == ">=":
            return a >= b
        raise PineError(f"unknown operator {op}")

    def _history(self, node: Node) -> Any:
        base, index_node = node.children
        offset = int(_num(self.eval(index_node)))
        if base.kind == "ident":
            name = base.value
            if name in ("close", "open", "high", "low", "volume") and not (
                (self.frame and name in self.frame) or name in self.vars
            ):
                j = self.i - offset
                return float(getattr(self.bars, name)[j]) if j >= 0 else NA
            if name in ("bar_index", "time"):
                j = self.i - offset
                if j < 0:
                    return NA
                return float(j) if name == "bar_index" else float(self.time_ms[j])
            if self.frame is not None and name in self.frame:
                raise PineError(f"history of function-local '{name}' is not implemented")
            if offset == 0:
                return self._lookup(name)
            series = self.history.get(name, [])
            j = len(series) - offset
            return series[j] if 0 <= j < len(series) else NA
        # history of an expression: a per-call-site buffer, appended each bar it is evaluated
        key = ("hist", self.scope, node.site)
        st = self.state.setdefault(key, {"buf": [], "last": -1})
        value = self.eval(base)
        if st["last"] == self.i:
            st["buf"][-1] = value
        else:
            st["buf"].append(value)
            st["last"] = self.i
        buf = st["buf"]
        j = len(buf) - 1 - offset
        return buf[j] if 0 <= j < len(buf) else NA

    # -------------------------------------------------------------- builtins
    def _call(self, node: Node) -> Any:
        name = node.value
        if name in NOOP_CALLS or name.startswith(NOOP_PREFIXES):
            return NA
        if name in DRAWING_NEW:
            self._next_id += 1
            return float(self._next_id)
        user = self.functions.get(name)
        if user is not None:
            return self._call_user(user, node)
        handler = _BUILTIN_FUNCS.get(name)
        if handler is not None:
            return handler(self, node)
        if name in STRATEGY_CALLS:
            return self._strategy_call(name, node)
        if name == "input" or name.startswith("input."):
            return self._input(node)
        head, _, rest = name.partition(".")
        if rest and "." not in rest:
            try:
                target = self._lookup(head)
            except PineError:
                target = None
            if isinstance(target, list):
                handler = _BUILTIN_FUNCS.get(f"array.{rest}")
                if handler is not None:
                    return handler(self, Node("call", f"array.{rest}", [Node("ident", head), *node.children], node.site))
        raise PineError(f"unimplemented Pine function {name!r}")

    def _call_user(self, fn: Stmt, node: Node) -> Any:
        params = fn.extra["params"]
        positional = [c for c in node.children if c.kind != "kwarg"]
        named = {c.value: c.children[0] for c in node.children if c.kind == "kwarg"}
        if len(positional) > len(params):
            raise PineError(f"{fn.target}() takes {len(params)} arguments, got {len(positional)}")
        frame: dict[str, Any] = {}
        for idx, (pname, default) in enumerate(params):
            if idx < len(positional):
                frame[pname] = self.eval(positional[idx])
            elif pname in named:
                frame[pname] = self.eval(named[pname])
            elif default is not None:
                frame[pname] = self.eval(default)
            else:
                raise PineError(f"{fn.target}() missing argument {pname!r}")
        saved_frame, saved_scope = self.frame, self.scope
        self.frame = frame
        self.scope = saved_scope + (node.site,)
        try:
            if fn.extra.get("inline"):
                return self.eval(fn.expr)
            return self._exec_block(fn.body)
        finally:
            self.frame, self.scope = saved_frame, saved_scope

    def _input(self, node: Node) -> Any:
        positional = [c for c in node.children if c.kind != "kwarg"]
        named = {c.value: c.children[0] for c in node.children if c.kind == "kwarg"}
        title = None
        if "title" in named:
            title = self.eval(named["title"])
        elif len(positional) > 1:
            title = self.eval(positional[1])
        if title is not None and title in self.inputs:
            return self.inputs[title]
        if "defval" in named:
            return self.eval(named["defval"])
        if positional:
            return self.eval(positional[0])
        return NA

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

    def _key(self, tag: str, node: Node) -> tuple:
        return (tag, self.scope, node.site)

    # ---- stateful series helpers
    def _series_buffer(self, key, node: Node) -> list[float]:
        """Values of ``node`` on every bar this call site ran. A built-in called
        conditionally sees only the bars it ran on -- exactly as in Pine."""
        st = self.state.get(key)
        if st is None:
            st = self.state[key] = {"buf": [], "last": -1}
        value = _num0(self.eval(node))
        if st["last"] == self.i:
            st["buf"][-1] = value
        else:
            st["buf"].append(value)
            st["last"] = self.i
        return st["buf"]

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
        return float(self.time_ms[self.i]) if self.session_cache[key][self.i] else NA

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
            self.session_cache[key] = np.asarray([float(pd_ts.value // 10**6) for pd_ts in stamps], dtype=float)
        return float(self.session_cache[key][self.i])

    def _true_range_buffer(self) -> list[float]:
        buf = self.state.setdefault(("tr", self.scope), [])
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


# ------------------------------------------------------------ builtin table
def _positional(node: Node) -> list[Node]:
    return [c for c in node.children if c.kind != "kwarg"]


def _named(node: Node) -> dict[str, Node]:
    return {c.value: c.children[0] for c in node.children if c.kind == "kwarg"}


def _bar_local(rt: PineRuntime, attr: str) -> float:
    dt = datetime.fromtimestamp(rt.time_ms[rt.i] / 1000.0, _zone(rt.tz))
    return float(getattr(dt, attr))


_BUILTIN_VARS: dict[str, Any] = {
    "close": lambda rt: float(rt.bars.close[rt.i]),
    "open": lambda rt: float(rt.bars.open[rt.i]),
    "high": lambda rt: float(rt.bars.high[rt.i]),
    "low": lambda rt: float(rt.bars.low[rt.i]),
    "volume": lambda rt: float(rt.bars.volume[rt.i]),
    "hl2": lambda rt: (rt.bars.high[rt.i] + rt.bars.low[rt.i]) / 2,
    "hlc3": lambda rt: (rt.bars.high[rt.i] + rt.bars.low[rt.i] + rt.bars.close[rt.i]) / 3,
    "ohlc4": lambda rt: (rt.bars.open[rt.i] + rt.bars.high[rt.i] + rt.bars.low[rt.i] + rt.bars.close[rt.i]) / 4,
    "time": lambda rt: float(rt.time_ms[rt.i]),
    "bar_index": lambda rt: float(rt.i),
    "last_bar_index": lambda rt: float(rt.n - 1),
    "dayofweek": lambda rt: float(rt.bars.weekday[rt.i] + 2 if rt.bars.weekday[rt.i] < 6 else 1),
    "hour": lambda rt: _bar_local(rt, "hour"),
    "minute": lambda rt: _bar_local(rt, "minute"),
    "syminfo.mintick": lambda rt: rt.mintick,
    "syminfo.tickerid": lambda rt: str(rt.bars.symbol),
    "syminfo.ticker": lambda rt: str(rt.bars.symbol),
    "syminfo.timezone": lambda rt: rt.tz,
    "timeframe.period": lambda rt: rt.bars.timeframe,
    "barstate.isconfirmed": lambda rt: True,  # backtest: every historical bar is closed
    "barstate.islast": lambda rt: rt.i == rt.n - 1,
    "barstate.isfirst": lambda rt: rt.i == 0,
    "barstate.isrealtime": lambda rt: False,
    "barstate.ishistory": lambda rt: True,
    "session.islastbar": lambda rt: (
        rt.i == rt.n - 1 or rt.bars.local_date[rt.i] != rt.bars.local_date[min(rt.i + 1, rt.n - 1)]
    ),
    "strategy.position_size": lambda rt: rt.position_size,
    "strategy.position_avg_price": lambda rt: rt.position_avg_price,
    "strategy.long": lambda rt: "long",
    "strategy.short": lambda rt: "short",
    "na": lambda rt: NA,
}


def _f_na(rt, node):
    return _isna(rt.eval(node.children[0]))


def _f_nz(rt, node):
    args = node.children
    v = rt.eval(args[0])
    return (rt.eval(args[1]) if len(args) > 1 else 0.0) if _isna(v) else v


def _f_minmax(rt, node):
    values = [_num(rt.eval(a)) for a in _positional(node)]
    if any(math.isnan(v) for v in values):
        return NA
    return max(values) if node.value.endswith("max") else min(values)


def _f_math1(fn):
    def call(rt, node):
        v = rt.eval(node.children[0])
        return NA if _isna(v) else float(fn(_num(v)))

    return call


def _f_round(rt, node):
    args = _positional(node)
    v = rt.eval(args[0])
    if _isna(v):
        return NA
    digits = int(_num(rt.eval(args[1]))) if len(args) > 1 else 0
    x = _num(v) * 10**digits
    # Pine rounds half away from zero, not to even.
    r = math.floor(abs(x) + 0.5) * (1 if x >= 0 else -1)
    return float(r) / 10**digits


def _f_pow(rt, node):
    a, b = (rt.eval(c) for c in _positional(node)[:2])
    return NA if _isna(a) or _isna(b) else float(_num(a) ** _num(b))


def _f_tostring(rt, node):
    v = rt.eval(node.children[0])
    if _isna(v):
        return "NaN"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _f_time(rt, node):
    args = _positional(node)
    if len(args) >= 2:
        sess = rt.eval(args[1])
        tz = rt.eval(args[2]) if len(args) > 2 else rt.tz
        return rt._session_time(str(sess), str(tz))
    if len(args) == 1:
        # time("D") is the OPENING time of the containing period, not the bar's
        # own timestamp. Getting this wrong makes ta.change(time("D")) fire on
        # every bar, which silently resets every `var` in the script.
        return rt._period_open(str(rt.eval(args[0])))
    return float(rt.time_ms[rt.i])


def _f_clock(attr: str):
    def call(rt, node):
        args = _positional(node)
        t = rt.eval(args[0]) if args else rt.time_ms[rt.i]
        if _isna(t):
            return NA
        tz = str(rt.eval(args[1])) if len(args) > 1 else rt.tz
        dt = datetime.fromtimestamp(_num(t) / 1000.0, _zone(tz))
        if attr == "dayofweek":
            return float((dt.weekday() + 1) % 7 + 1)
        return float(getattr(dt, attr))

    return call


def _f_color(rt, node):
    return "color"


def _f_cast(kind):
    def call(rt, node):
        v = rt.eval(node.children[0])
        if _isna(v):
            return NA
        if kind == "int":
            return float(math.trunc(_num(v)))
        if kind == "bool":
            return _truthy(v)
        return _num(v)

    return call


def _f_change(rt, node):
    args = _positional(node)
    length = int(_num(rt.eval(args[1]))) if len(args) > 1 else 1
    buf = rt._series_buffer(rt._key("chg", node), args[0])
    if len(buf) <= length:
        return NA
    return buf[-1] - buf[-1 - length]


def _f_cum(rt, node):
    key = rt._key("cum", node)
    acc = rt.state.get(key, 0.0) + _num0(rt.eval(node.children[0]))
    rt.state[key] = acc
    return acc


def _f_cross(rt, node):
    name = node.value
    a = rt._series_buffer(rt._key("xa", node), node.children[0])
    b = rt._series_buffer(rt._key("xb", node), node.children[1])
    if len(a) < 2 or len(b) < 2:
        return False
    if any(math.isnan(x) for x in (a[-1], a[-2], b[-1], b[-2])):
        return False
    if name == "ta.crossover":
        return a[-2] <= b[-2] and a[-1] > b[-1]
    return a[-2] >= b[-2] and a[-1] < b[-1]


def _f_series(rt, node):
    name = node.value
    args = _positional(node)
    if name == "ta.atr":
        length = int(_num(rt.eval(args[0])))
        buf = rt._true_range_buffer()
        return _rma_step(rt.state, rt._key("atr", node), buf, length, rt.i)
    if name == "ta.vwap":
        return _vwap(rt, node)
    if name == "ta.rsi":
        length = int(_num(rt.eval(args[1])))
        src = rt._series_buffer(rt._key("rsisrc", node), args[0])
        return _rsi_step(rt.state, rt._key("rsi", node), src, length, len(src) - 1)
    src = rt._series_buffer(rt._key("src", node), args[0])
    length = int(_num(rt.eval(args[1])))
    i = len(src) - 1
    if name == "ta.sma":
        return _sma_step(src, length, i)
    if name == "ta.ema":
        return _ema_step(rt.state, rt._key("ema", node), src, length, i)
    if name == "ta.rma":
        return _rma_step(rt.state, rt._key("rma", node), src, length, i)
    if name == "ta.stdev":
        return _stdev_step(src, length, i)
    if name == "ta.highest":
        window = src[max(0, i - length + 1): i + 1]
        return max(window) if len(window) == length else NA
    if name == "ta.lowest":
        window = src[max(0, i - length + 1): i + 1]
        return min(window) if len(window) == length else NA
    raise PineError(f"unimplemented series function {name}")


def _f_pivot(rt, node):
    """``ta.pivothigh`` / ``ta.pivotlow`` with TradingView's tie rule: an equal
    value to the LEFT of the candidate does not disqualify it, an equal value to
    the RIGHT does. Of two equal highs, the later one is the pivot."""
    is_high = node.value == "ta.pivothigh"
    args = _positional(node)
    if len(args) == 2:
        src_node = Node("ident", "high" if is_high else "low")
        left_n, right_n = args
    elif len(args) == 3:
        src_node, left_n, right_n = args
    else:
        raise PineError(f"{node.value} takes 2 or 3 arguments")
    left, right = int(_num(rt.eval(left_n))), int(_num(rt.eval(right_n)))
    buf = rt._series_buffer(rt._key("pivot", node), src_node)
    i = len(buf) - 1
    c = i - right
    if c - left < 0:
        return NA
    center = buf[c]
    if math.isnan(center):
        return NA
    for j in range(c - left, c):
        v = buf[j]
        if math.isnan(v) or (v > center if is_high else v < center):
            return NA
    for j in range(c + 1, i + 1):
        v = buf[j]
        if math.isnan(v) or (v >= center if is_high else v <= center):
            return NA
    return center


def _vwap(rt, node):
    key = rt._key("vwap", node)
    state = rt.state.setdefault(key, {"day": None, "pv": 0.0, "vv": 0.0, "last": -1})
    if state["last"] == rt.i:
        return state["value"]
    day = rt.bars.local_date[rt.i]
    if day != state["day"]:
        state.update({"day": day, "pv": 0.0, "vv": 0.0})
    src = _num0(rt.eval(node.children[0]))
    state["pv"] += src * rt.bars.volume[rt.i]
    state["vv"] += rt.bars.volume[rt.i]
    state["value"] = state["pv"] / state["vv"] if state["vv"] > 0 else NA
    state["last"] = rt.i
    return state["value"]


def _f_security(rt, node):
    """``request.security`` on an intraday higher timeframe, lookahead off.

    The expression is evaluated on the higher-timeframe bars (built from the
    chart's own bars) by a child runtime with its own built-in state, and each
    chart bar receives the value of the latest higher-timeframe bar that had
    closed by its own close -- :mod:`ee_agent.data.htf`, shared with the Python
    primitives so the two paths cannot disagree about timing.
    """
    from ee_agent.data.htf import htf_view
    from ee_agent.errors import PrimitiveError

    key = rt._key("security", node)
    cache = rt.state.get(key)
    if cache is None:
        args = _positional(node)
        named = _named(node)
        tf_node = args[1] if len(args) > 1 else named.get("timeframe")
        expr = args[2] if len(args) > 2 else named.get("expression")
        if tf_node is None or expr is None:
            raise PineError("request.security needs a timeframe and an expression")
        lookahead = named.get("lookahead")
        if lookahead is not None and str(rt.eval(lookahead)).endswith("lookahead_on"):
            raise PineError(
                "request.security with lookahead_on reads a higher-timeframe bar before it closes. "
                "On historical bars that is future data; this interpreter refuses to run it."
            )
        tf = str(rt.eval(tf_node))
        try:
            htf, kmap = htf_view(rt.bars, tf)
        except PrimitiveError as exc:
            raise PineError(str(exc)) from exc
        child = PineRuntime(htf, rt.mintick, rt.tz, version=rt.version, inputs=rt.inputs, functions=rt.functions)
        child.vars = dict(rt.vars)
        child.persistent = set(child.vars)
        values = []
        for k in range(len(htf)):
            child.i = k
            values.append(child.eval(expr))
        cache = rt.state[key] = (values, kmap)
    values, kmap = cache
    k = int(kmap[rt.i])
    if k < 0:
        sample = values[0] if values else NA
        return tuple(NA for _ in sample) if isinstance(sample, tuple) else NA
    return values[k]


def _f_security_ltf(rt, node):
    from ee_agent.data.bars import timeframe_minutes
    from ee_agent.data.htf import pine_timeframe_minutes
    from ee_agent.errors import PrimitiveError

    args = _positional(node)
    named = _named(node)
    tf_node = args[1] if len(args) > 1 else named.get("timeframe")
    expr = args[2] if len(args) > 2 else named.get("expression")
    key = rt._key("security_ltf", node)
    if key not in rt.state:
        tf = str(rt.eval(tf_node))
        try:
            minutes = pine_timeframe_minutes(tf)
        except PrimitiveError as exc:
            raise PineError(str(exc)) from exc
        if minutes > timeframe_minutes(rt.bars.timeframe):
            raise PineError(f"request.security_lower_tf asked for {tf}, which is above the chart timeframe")
        note = (
            f"request.security_lower_tf('{tf}') returned one element per {rt.bars.timeframe} bar "
            "(the chart bar's own value). Exact for values read at a chart-bar boundary; "
            "anything read inside a chart bar would need lower-timeframe data."
        )
        if note not in rt.notes:
            rt.notes.append(note)
        rt.state[key] = True
    return [rt.eval(expr)]


# ---- arrays
def _arr(rt, node, idx=0) -> list:
    value = rt.eval(_positional(node)[idx])
    if not isinstance(value, list):
        raise PineError(f"{node.value}: argument is not an array")
    return value


def _index(rt, arr: list, node: Node, idx_node: Node) -> int:
    k = int(_num(rt.eval(idx_node)))
    if k < -len(arr) or k >= len(arr):
        raise PineError(f"{node.value}: index {k} is out of bounds for an array of size {len(arr)}")
    return k


def _f_array_new(rt, node):
    args = _positional(node)
    size = int(_num(rt.eval(args[0]))) if args else 0
    init = rt.eval(args[1]) if len(args) > 1 else NA
    return [init] * size


def _f_array_from(rt, node):
    return [rt.eval(a) for a in _positional(node)]


def _f_array_push(rt, node):
    _arr(rt, node).append(rt.eval(_positional(node)[1]))
    return NA


def _f_array_unshift(rt, node):
    _arr(rt, node).insert(0, rt.eval(_positional(node)[1]))
    return NA


def _f_array_get(rt, node):
    arr = _arr(rt, node)
    return arr[_index(rt, arr, node, _positional(node)[1])]


def _f_array_set(rt, node):
    arr = _arr(rt, node)
    args = _positional(node)
    arr[_index(rt, arr, node, args[1])] = rt.eval(args[2])
    return NA


def _f_array_size(rt, node):
    return float(len(_arr(rt, node)))


def _f_array_pop(rt, node):
    arr = _arr(rt, node)
    if not arr:
        raise PineError("array.pop on an empty array")
    return arr.pop()


def _f_array_shift(rt, node):
    arr = _arr(rt, node)
    if not arr:
        raise PineError("array.shift on an empty array")
    return arr.pop(0)


def _f_array_remove(rt, node):
    arr = _arr(rt, node)
    return arr.pop(_index(rt, arr, node, _positional(node)[1]))


def _f_array_insert(rt, node):
    arr = _arr(rt, node)
    args = _positional(node)
    arr.insert(int(_num(rt.eval(args[1]))), rt.eval(args[2]))
    return NA


def _f_array_clear(rt, node):
    _arr(rt, node).clear()
    return NA


def _f_array_first(rt, node):
    arr = _arr(rt, node)
    if not arr:
        raise PineError("array.first on an empty array")
    return arr[0]


def _f_array_last(rt, node):
    arr = _arr(rt, node)
    if not arr:
        raise PineError("array.last on an empty array")
    return arr[-1]


def _f_array_includes(rt, node):
    return rt.eval(_positional(node)[1]) in _arr(rt, node)


_BUILTIN_FUNCS: dict[str, Any] = {
    "na": _f_na,
    "nz": _f_nz,
    "math.max": _f_minmax,
    "math.min": _f_minmax,
    "math.abs": _f_math1(abs),
    "math.sqrt": _f_math1(math.sqrt),
    "math.floor": _f_math1(math.floor),
    "math.ceil": _f_math1(math.ceil),
    "math.log": _f_math1(math.log),
    "math.exp": _f_math1(math.exp),
    "math.sign": _f_math1(lambda x: (x > 0) - (x < 0)),
    "math.round": _f_round,
    "math.pow": _f_pow,
    "int": _f_cast("int"),
    "float": _f_cast("float"),
    "bool": _f_cast("bool"),
    "str.tostring": _f_tostring,
    "time": _f_time,
    "hour": _f_clock("hour"),
    "minute": _f_clock("minute"),
    "second": _f_clock("second"),
    "dayofmonth": _f_clock("day"),
    "month": _f_clock("month"),
    "year": _f_clock("year"),
    "dayofweek": _f_clock("dayofweek"),
    "color.new": _f_color,
    "color.rgb": _f_color,
    "color.from_gradient": _f_color,
    "ta.change": _f_change,
    "ta.cum": _f_cum,
    "ta.crossover": _f_cross,
    "ta.crossunder": _f_cross,
    "ta.pivothigh": _f_pivot,
    "ta.pivotlow": _f_pivot,
    "request.security": _f_security,
    "request.security_lower_tf": _f_security_ltf,
    "array.new": _f_array_new,
    "array.from": _f_array_from,
    "array.push": _f_array_push,
    "array.unshift": _f_array_unshift,
    "array.get": _f_array_get,
    "array.set": _f_array_set,
    "array.size": _f_array_size,
    "array.pop": _f_array_pop,
    "array.shift": _f_array_shift,
    "array.remove": _f_array_remove,
    "array.insert": _f_array_insert,
    "array.clear": _f_array_clear,
    "array.first": _f_array_first,
    "array.last": _f_array_last,
    "array.includes": _f_array_includes,
}
for _t in ("float", "int", "bool", "string", "color", "box", "line", "label", "table", "linefill"):
    _BUILTIN_FUNCS[f"array.new_{_t}"] = _f_array_new
for _n in ("ta.ema", "ta.sma", "ta.rma", "ta.stdev", "ta.atr", "ta.rsi", "ta.vwap", "ta.highest", "ta.lowest"):
    _BUILTIN_FUNCS[_n] = _f_series


# ------------------------------------------------------------------- numerics
def _sma_step(buf: list[float], length: int, i: int) -> float:
    if i + 1 < length:
        return NA
    window = buf[i - length + 1: i + 1]
    if any(math.isnan(x) for x in window):
        return NA
    return sum(window) / length


def _stdev_step(buf: list[float], length: int, i: int) -> float:
    if i + 1 < length:
        return NA
    window = buf[i - length + 1: i + 1]
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
        seed = buf[i - length + 1: i + 1]
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
        value = sum(buf[i - length + 1: i + 1]) / length
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
    g = _rma_step(state, (key, "rg"), gains, length, len(gains) - 1)
    l = _rma_step(state, (key, "rl"), losses, length, len(losses) - 1)
    if math.isnan(g) or math.isnan(l):
        value = NA
    elif l == 0:
        value = 100.0
    else:
        value = 100.0 - (100.0 / (1.0 + g / l))
    st.update({"i": i, "value": value})
    return value


def _isna(v: Any) -> bool:
    return isinstance(v, float) and v != v


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None or _isna(v):
        return False
    if isinstance(v, (int, float)):
        return v != 0
    return bool(v)


def _num(v: Any) -> float:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if v is None:
        return NA
    if isinstance(v, (str, list, tuple)):
        raise PineError(f"cannot use {v!r} as a number")
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


def run_pine(
    source: str,
    bars,
    collect: list[str],
    mintick: float = 0.25,
    inputs: dict | None = None,
    notes: list[str] | None = None,
) -> dict[str, np.ndarray]:
    """Parse and execute Pine over ``bars``, returning the named series.

    ``inputs`` overrides ``input.*`` defaults by title (or by variable name).
    Anything the run had to approximate is appended to ``notes``.
    """
    program = parse_program(source)
    rt = PineRuntime(bars, mintick=mintick, version=_version(source), inputs=inputs)
    out = rt.run(program, collect)
    if notes is not None:
        notes.extend(n for n in rt.notes if n not in notes)
    return out
