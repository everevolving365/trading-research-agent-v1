"""A scripted model, so the conversation is testable with zero credentials.

Section 2.2: a client's model key is a runtime input, not a build dependency.
This plays a fixed script of replies and tool calls, which means the whole
conversational path -- tool dispatch, results feeding back, the transcript, the
cost ledger -- is verified end to end without anyone's API key.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ee_agent.conversation.model import Message, ModelClient, ModelReply


@dataclass
class ScriptedModel(ModelClient):
    """Replays ``script``: each entry is either text, or tool calls to make."""

    script: list[dict] = field(default_factory=list)
    name: str = "scripted"
    model: str = "scripted-test"
    calls_seen: list[list[Message]] = field(default_factory=list)
    index: int = 0

    @property
    def available(self) -> bool:
        return True

    def complete(self, messages: list[Message], tools: list[dict] | None = None, **kw) -> ModelReply:
        self.calls_seen.append(list(messages))
        if self.index >= len(self.script):
            return ModelReply(text="(script exhausted)", model=self.model, input_tokens=10, output_tokens=5)
        step = self.script[self.index]
        self.index += 1
        calls = [
            {"id": f"call_{self.index}_{i}", "name": c["name"], "arguments": c.get("arguments", {})}
            for i, c in enumerate(step.get("tools", []))
        ]
        reply = ModelReply(
            text=step.get("text", ""),
            tool_calls=calls,
            input_tokens=step.get("input_tokens", 100),
            output_tokens=step.get("output_tokens", 50),
            model=self.model,
        )
        self.price(reply)
        return reply

    @property
    def tool_results_seen(self) -> list[str]:
        """Every tool result fed back into the conversation, once each, in order.

        Each entry of ``calls_seen`` is a full snapshot of the message list, so
        the same tool result appears in every later snapshot. Reading the last
        snapshot gives each result exactly once.
        """
        if not self.calls_seen:
            return []
        return [m.content for m in self.calls_seen[-1] if m.role == "tool"]


class ExplodingModel(ModelClient):
    """Fails on every call, to prove the conversation degrades rather than crashes."""

    name = "exploding"
    model = "exploding"

    @property
    def available(self) -> bool:
        return True

    def complete(self, messages: list[Message], tools: list[dict] | None = None, **kw) -> ModelReply:
        raise RuntimeError("simulated provider outage")
