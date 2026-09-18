"""Model clients. Bring your own key, any provider, or none at all.

Every client supplies their own model API key (Section 1.3). The agent never
ships with anyone else's credentials and the owner carries zero per-user cost.

With no key at all the :class:`NullModel` still answers: it routes to the
agent's own deterministic capabilities and says plainly what it cannot do
without a model. Nothing in the system is gated behind a paid key.
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator

from ee_agent.cost.notifier import ledger
from ee_agent.secrets.vault import get_secret


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.name:
            out["name"] = self.name
        return out


@dataclass
class ModelReply:
    text: str
    tool_calls: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    stop_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ModelClient(ABC):
    name: str = "abstract"
    model: str = ""

    @property
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def complete(self, messages: list[Message], tools: list[dict] | None = None, **kw) -> ModelReply: ...

    def price(self, reply: ModelReply) -> float:
        """Record what that turn cost. The cost layer informs, never blocks."""
        record = ledger().spend(
            "conversation",
            **{
                "model.input_1k": reply.input_tokens / 1000.0,
                "model.output_1k": reply.output_tokens / 1000.0,
            },
        )
        return record.actual_usd


class NullModel(ModelClient):
    """No key configured. Still useful, and honest about the boundary."""

    name = "none"
    model = "none"

    @property
    def available(self) -> bool:
        return True

    def complete(self, messages: list[Message], tools: list[dict] | None = None, **kw) -> ModelReply:
        return ModelReply(
            text=(
                "I have no model key configured, so I cannot hold an open-ended conversation. "
                "Everything else still works: describing your strategy, backtesting it, compiling "
                "the indicator, proving parity, running the Operator. Add a key with "
                "`ee-agent secrets set ANTHROPIC_API_KEY` (or GEMINI_API_KEY / OPENAI_API_KEY) and "
                "I can talk about anything."
            ),
            model="none",
            stop_reason="no_model",
        )


class AnthropicModel(ModelClient):
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-5", max_tokens: int = 4096):
        self.model = model
        self.max_tokens = max_tokens

    @property
    def available(self) -> bool:
        return bool(get_secret("ANTHROPIC_API_KEY"))

    def complete(self, messages: list[Message], tools: list[dict] | None = None, **kw) -> ModelReply:
        import anthropic  # imported lazily: never a hard dependency

        client = anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        payload = _to_anthropic(messages)
        request: dict[str, Any] = {
            "model": kw.get("model", self.model),
            "max_tokens": kw.get("max_tokens", self.max_tokens),
            "messages": payload,
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["parameters"],
                }
                for t in tools
            ]
        response = client.messages.create(**request)

        text_parts, calls = [], []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append({"id": block.id, "name": block.name, "arguments": block.input})
        reply = ModelReply(
            text="\n".join(text_parts).strip(),
            tool_calls=calls,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
            stop_reason=response.stop_reason or "",
        )
        self.price(reply)
        return reply


class OpenAIModel(ModelClient):
    name = "openai"

    def __init__(self, model: str = "gpt-4o", max_tokens: int = 4096):
        self.model = model
        self.max_tokens = max_tokens

    @property
    def available(self) -> bool:
        return bool(get_secret("OPENAI_API_KEY"))

    def complete(self, messages: list[Message], tools: list[dict] | None = None, **kw) -> ModelReply:
        from openai import OpenAI

        client = OpenAI(api_key=get_secret("OPENAI_API_KEY"))
        request: dict[str, Any] = {
            "model": kw.get("model", self.model),
            "messages": [m.to_dict() for m in messages],
            "max_tokens": kw.get("max_tokens", self.max_tokens),
        }
        if tools:
            request["tools"] = [{"type": "function", "function": t} for t in tools]
        response = client.chat.completions.create(**request)
        choice = response.choices[0]
        calls = [
            {"id": c.id, "name": c.function.name, "arguments": json.loads(c.function.arguments or "{}")}
            for c in (choice.message.tool_calls or [])
        ]
        reply = ModelReply(
            text=choice.message.content or "",
            tool_calls=calls,
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
            model=response.model,
            stop_reason=choice.finish_reason or "",
        )
        self.price(reply)
        return reply


class GeminiModel(ModelClient):
    name = "gemini"

    def __init__(self, model: str = "gemini-2.0-flash", max_tokens: int = 4096):
        self.model = model
        self.max_tokens = max_tokens

    @property
    def available(self) -> bool:
        return bool(get_secret("GEMINI_API_KEY"))

    def complete(self, messages: list[Message], tools: list[dict] | None = None, **kw) -> ModelReply:
        import google.generativeai as genai

        genai.configure(api_key=get_secret("GEMINI_API_KEY"))
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        model = genai.GenerativeModel(kw.get("model", self.model), system_instruction=system or None)
        history = [
            {"role": "user" if m.role == "user" else "model", "parts": [m.content]}
            for m in messages
            if m.role in ("user", "assistant") and m.content
        ]
        response = model.generate_content(history)
        usage = getattr(response, "usage_metadata", None)
        reply = ModelReply(
            text=response.text or "",
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
            model=self.model,
        )
        self.price(reply)
        return reply


def _to_anthropic(messages: list[Message]) -> list[dict]:
    """Translate the internal message list into Anthropic's block format."""
    out: list[dict] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                    ],
                }
            )
            continue
        if m.role == "assistant" and m.tool_calls:
            blocks: list[dict] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for call in m.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call.get("arguments", {}),
                    }
                )
            out.append({"role": "assistant", "content": blocks})
            continue
        if m.content:
            out.append({"role": m.role, "content": m.content})
    return out


PROVIDERS: dict[str, type[ModelClient]] = {
    "anthropic": AnthropicModel,
    "openai": OpenAIModel,
    "gemini": GeminiModel,
}


def get_model(provider: str | None = None, model: str | None = None) -> ModelClient:
    """Pick a provider. Explicit choice wins; otherwise the first key present.

    Returns :class:`NullModel` rather than raising when nothing is configured --
    the zero-cost floor must never hit an exception.
    """
    if provider and provider not in ("none", "auto"):
        cls = PROVIDERS.get(provider)
        if cls is None:
            raise KeyError(f"Unknown model provider {provider!r}. Known: {', '.join(PROVIDERS)}")
        client = cls(model=model) if model else cls()
        return client if client.available else NullModel()
    if provider == "none":
        return NullModel()
    for cls in (AnthropicModel, OpenAIModel, GeminiModel):
        candidate = cls(model=model) if model else cls()
        if candidate.available:
            return candidate
    return NullModel()


def describe_models() -> str:
    lines = ["[models] bring your own key -- the agent ships with none:"]
    for name, cls in PROVIDERS.items():
        client = cls()
        mark = "READY" if client.available else "no key"
        lines.append(f"    {name:<12} {client.model:<24} {mark}")
    active = get_model()
    lines.append(f"    active: {active.name}")
    if active.name == "none":
        lines.append(
            "    No key configured. Backtesting, compiling, parity, ingestion and the Operator all "
            "still work -- only open-ended conversation needs a model."
        )
    return "\n".join(lines)
