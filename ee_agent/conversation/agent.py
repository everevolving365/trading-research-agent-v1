"""The conversation: deep, open-ended, and able to operate the whole agent.

The client talks about whatever they want -- their strategy, the market, why
last Tuesday went badly, or something with nothing to do with trading. When the
conversation calls for work, the agent does the work rather than describing it:
it loads the data, runs the backtest, compiles the indicator, drives the browser.

Voice is a shell over this, never a dependency (:mod:`ee_agent.capture.voice`).
With no model key it still runs and says plainly what it cannot do.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from ee_agent.conversation import tools as toolbox
from ee_agent.conversation.model import Message, ModelClient, ModelReply, get_model
from ee_agent.cost.notifier import CeilingReached, ledger
from ee_agent.paths import ee_home

SYSTEM_PROMPT = """You are the EverEvolving trading agent, talking with the client who owns you.

WHO OWNS WHAT -- this is the rule you never break:
The client owns the strategy. The client owns the risk management. You do not
invent strategies, you do not set risk, and you never tell anyone what to trade.
They speak; you act on exactly what they said, without distortion. If you are
tempted to fill in a rule they did not give you, ask them instead.

You are not a chatbot with a trading skill bolted on. You have tools that
actually do the work. When the client asks for something, DO IT with the tools
and report what came back. Never describe what a backtest would show -- run it.
Never write Pine by hand -- compile it. Never guess at data -- load it.

HOW TO LISTEN
The client will ramble. That is expected and it is good -- more detail is more
signal. Let them talk. Take in everything, at any length, in any order. Pass
their words to describe_strategy VERBATIM, not summarised: the parser wants what
they actually said, including the asides.

Then interrogate. The tool returns `still_to_ask` -- work through every item,
one question at a time, in their language, until their strategy is completely
unambiguous. A strategy with a hole in it is a backtest that lies. Do not move
on to backtesting while items remain unanswered, and do not answer them yourself.

HOW TO REPORT
Every result carries the case against it. Read the adversarial findings out
plainly. If the strategy does not work, say so -- that is the most valuable
thing you will ever tell them. Never soften a number, never bury a drawdown, and
never report a single-window result as if it were defensible.

Show them data constantly. Charts, numbers, bar counts, cache age, quality
scores. They should never wonder what you are working from.

COST
You never refuse on cost grounds and you never hard-stop. Before anything
expensive you say what it will cost and why, then you proceed. If they set their
own ceiling and it is reached, you tell them and ask -- you do not stop silently.

CONVERSATION
Talk about anything they want, trading or not. Be direct, warm and concrete.
Short answers to short questions. No filler, no restating their question back at
them, no "great question". If you do not know something, say so.
"""


@dataclass
class Turn:
    role: str
    text: str
    tools_used: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Conversation:
    """A durable, tool-using conversation. Transcript survives the session."""

    def __init__(
        self,
        model: ModelClient | None = None,
        max_tool_rounds: int = 8,
        transcript_path: Path | None = None,
        sink: Callable[[str], None] = print,
        system_prompt: str = SYSTEM_PROMPT,
    ):
        self.model = model or get_model()
        self.max_tool_rounds = max_tool_rounds
        self.sink = sink
        self.messages: list[Message] = [Message("system", system_prompt)]
        self.turns: list[Turn] = []
        self.transcript_path = transcript_path or (
            ee_home() / "conversations" / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        )

    # ------------------------------------------------------------- talking
    def say(self, text: str, on_tool: Callable[[str, dict], None] | None = None) -> str:
        """One client turn. Runs tools as needed and returns what to say back."""
        self.messages.append(Message("user", text))
        self._record(Turn("client", text))
        used: list[str] = []
        spend_before = ledger().total_usd

        for _round in range(self.max_tool_rounds):
            try:
                reply = self.model.complete(self.messages, tools=toolbox.schemas())
            except CeilingReached as exc:
                answer = str(exc)
                self._record(Turn("agent", answer))
                return answer
            except Exception as exc:
                answer = (
                    f"I could not reach the model ({type(exc).__name__}: {exc}). "
                    "Everything that does not need a model still works -- try `ee-agent analyze`, "
                    "`ee-agent compile` or `ee-agent parity`."
                )
                self._record(Turn("agent", answer))
                return answer

            if not reply.wants_tools:
                answer = reply.text or "(no answer)"
                self.messages.append(Message("assistant", answer))
                self._record(
                    Turn("agent", answer, used, round(ledger().total_usd - spend_before, 6))
                )
                return answer

            self.messages.append(Message("assistant", reply.text, tool_calls=reply.tool_calls))
            for call in reply.tool_calls:
                name, arguments = call["name"], call.get("arguments", {})
                used.append(name)
                if on_tool:
                    on_tool(name, arguments)
                else:
                    self.sink(f"  [{name}] {_brief(arguments)}")
                result = toolbox.call(name, arguments)
                self.messages.append(
                    Message("tool", result, tool_call_id=call.get("id", ""), name=name)
                )

        answer = (
            "I used a lot of tools on that without landing on an answer. Tell me which part "
            "matters most and I will go straight at it."
        )
        self._record(Turn("agent", answer, used))
        return answer

    # --------------------------------------------------------------- state
    def _record(self, turn: Turn) -> None:
        self.turns.append(turn)
        try:
            self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
            with self.transcript_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(turn.__dict__, default=str) + "\n")
        except OSError:
            pass

    def greeting(self) -> str:
        from ee_agent.library import loader

        lines = [
            "I'm your trading agent. Tell me about your strategy -- as much detail as you like, "
            "in any order. I'll ask about anything you leave out.",
        ]
        if self.model.name == "none":
            lines.append(
                "Heads up: no model key is set, so I can't hold an open conversation yet. "
                "Everything else works. `ee-agent secrets set ANTHROPIC_API_KEY` fixes it."
            )
        if loader.should_offer():
            offer = loader.offer_text()
            if offer:
                lines.append(offer)
                loader.mark_offered()
        return "\n".join(lines)

    def summary(self) -> str:
        spend = sum(t.cost_usd for t in self.turns)
        return (
            f"[conversation] {len([t for t in self.turns if t.role == 'client'])} exchange(s), "
            f"{len({tool for t in self.turns for tool in t.tools_used})} tool(s) used, "
            f"${spend:,.4f} spent. Transcript: {self.transcript_path}"
        )


def _brief(arguments: dict) -> str:
    parts = []
    for key, value in (arguments or {}).items():
        text = str(value).replace("\n", " ")
        parts.append(f"{key}={text[:60] + ('...' if len(text) > 60 else '')}")
    return ", ".join(parts)[:160]


def run_repl(
    voice: bool = False,
    provider: str | None = None,
    input_fn: Callable[[str], str] = input,
    sink: Callable[[str], None] = print,
    max_turns: int | None = None,
) -> int:
    """The interactive loop. Voice is a shell over it and changes nothing else."""
    from ee_agent.capture.voice import VoiceShell

    shell = VoiceShell(enabled=voice, input_fn=input_fn)
    conversation = Conversation(model=get_model(provider), sink=sink)

    sink("")
    shell.say(conversation.greeting(), also_print=True)
    sink("")
    sink("  (type 'exit' to leave, 'spend' for the running total, 'status' for what's set up)")
    sink("")

    turns = 0
    while max_turns is None or turns < max_turns:
        turns += 1
        try:
            said = shell.ask("").strip() if voice else input_fn("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not said:
            continue
        lowered = said.lower()
        if lowered in ("exit", "quit", "bye"):
            break
        if lowered == "spend":
            sink(ledger().banner())
            continue
        if lowered == "status":
            sink(json.dumps(toolbox.call("agent_status", {}), indent=2)[:4000])
            continue

        answer = conversation.say(said)
        sink("")
        shell.say(answer, also_print=True)
        sink("")

    sink(conversation.summary())
    sink(ledger().banner())
    return 0
