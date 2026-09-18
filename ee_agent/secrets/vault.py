"""One interface for every credential in the system: ``get_secret(name)``.

Backed by the OS keychain when ``keyring`` is installed, falling back to an
AES-GCM encrypted file, falling back to the process environment. Nothing in this
repository ever ships a credential, and every code path that needs one must also
work against a fixture (see ``ee_agent.data.fixtures``).

Secrets are never logged. ``describe()`` reports presence, never value.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ee_agent.paths import ee_home

SERVICE = "everevolving-trading-agent"

#: Every secret the system can use, what it unlocks, and where to get it.
KNOWN_SECRETS: dict[str, tuple[str, str]] = {
    "ANTHROPIC_API_KEY": (
        "conversation, interrogation, adversarial pass",
        "https://console.anthropic.com/settings/keys",
    ),
    "GEMINI_API_KEY": ("alternative model provider", "https://aistudio.google.com/app/apikey"),
    "OPENAI_API_KEY": ("alternative model provider", "https://platform.openai.com/api-keys"),
    "POLYGON_API_KEY": (
        "stocks, ETFs, indices, forex minute history",
        "https://polygon.io/dashboard/api-keys",
    ),
    "DATABENTO_API_KEY": (
        "CME futures history, tick and order-flow data",
        "https://databento.com/portal/keys",
    ),
    "BINANCE_API_KEY": (
        "higher crypto rate limits (public klines need no key)",
        "https://www.binance.com/en/my/settings/api-management",
    ),
    "BINANCE_API_SECRET": (
        "paired with BINANCE_API_KEY",
        "https://www.binance.com/en/my/settings/api-management",
    ),
    "TRADINGVIEW_USERNAME": (
        "Operator login to the client's TradingView account",
        "the client's own TradingView account",
    ),
    "TRADINGVIEW_PASSWORD": (
        "Operator login to the client's TradingView account",
        "the client's own TradingView account",
    ),
    "TOPSTEPX_USERNAME": ("live execution on TopstepX", "https://topstepx.com"),
    "TOPSTEPX_API_KEY": ("live execution on TopstepX", "https://topstepx.com -> API access"),
    "TOPSTEPX_ACCOUNT_ID": ("which TopstepX account to trade", "the TopstepX dashboard"),
    "BRAVE_SEARCH_API_KEY": (
        "searching the web for data sources the agent does not already know",
        "https://brave.com/search/api/",
    ),
    "TAVILY_API_KEY": (
        "alternative web search for data-source discovery",
        "https://tavily.com",
    ),
    "SERPAPI_API_KEY": (
        "alternative web search for data-source discovery",
        "https://serpapi.com/manage-api-key",
    ),
}


class SecretNotFound(KeyError):
    def __init__(self, name: str):
        unlocks, where = KNOWN_SECRETS.get(name, ("(unknown secret)", "(unknown)"))
        super().__init__(
            f"{name} is not set. It unlocks: {unlocks}. Obtain it at: {where}. "
            f"Store it with `ee-agent secrets set {name}` (never pasted into a log)."
        )
        self.name = name


@dataclass(frozen=True)
class SecretStatus:
    name: str
    present: bool
    backend: str
    unlocks: str
    where: str


# --------------------------------------------------------------------- backends
class _KeychainBackend:
    name = "os-keychain"

    def __init__(self) -> None:
        try:
            import keyring  # type: ignore

            keyring.get_keyring()
            self._kr = keyring
        except Exception:  # pragma: no cover - depends on host
            self._kr = None

    @property
    def available(self) -> bool:
        return self._kr is not None

    def get(self, name: str) -> str | None:
        if not self.available:
            return None
        try:
            return self._kr.get_password(SERVICE, name)
        except Exception:  # pragma: no cover
            return None

    def set(self, name: str, value: str) -> None:
        self._kr.set_password(SERVICE, name, value)

    def delete(self, name: str) -> None:
        try:
            self._kr.delete_password(SERVICE, name)
        except Exception:
            pass


class _EncryptedFileBackend:
    """AES-GCM encrypted blob at ``$EE_HOME/secrets.enc``.

    The key is a machine-local key file (0600 where the OS supports it). This is
    a fallback for hosts without a keychain, not a hardware vault.
    """

    name = "encrypted-file"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (ee_home() / "secrets.enc")
        self.key_path = self.path.with_suffix(".key")

    @property
    def available(self) -> bool:
        try:
            import cryptography  # noqa: F401

            return True
        except Exception:
            return False

    def _key(self) -> bytes:
        if self.key_path.exists():
            return base64.urlsafe_b64decode(self.key_path.read_bytes())
        key = os.urandom(32)
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.write_bytes(base64.urlsafe_b64encode(key))
        _chmod600(self.key_path)
        return key

    def _load(self) -> dict[str, str]:
        if not self.path.exists() or not self.available:
            return {}
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        raw = self.path.read_bytes()
        nonce, blob = raw[:12], raw[12:]
        try:
            plain = AESGCM(self._key()).decrypt(nonce, blob, None)
        except Exception:
            return {}
        return json.loads(plain.decode("utf-8"))

    def _save(self, data: dict[str, str]) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        blob = AESGCM(self._key()).encrypt(nonce, json.dumps(data).encode("utf-8"), None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(nonce + blob)
        _chmod600(self.path)

    def get(self, name: str) -> str | None:
        return self._load().get(name)

    def set(self, name: str, value: str) -> None:
        data = self._load()
        data[name] = value
        self._save(data)

    def delete(self, name: str) -> None:
        data = self._load()
        if name in data:
            data.pop(name)
            self._save(data)


class _EnvBackend:
    name = "environment"
    available = True

    def get(self, name: str) -> str | None:
        return os.environ.get(name) or None

    def set(self, name: str, value: str) -> None:
        os.environ[name] = value

    def delete(self, name: str) -> None:
        os.environ.pop(name, None)


def _chmod600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows ACLs
        pass


# ----------------------------------------------------------------------- vault
class Vault:
    def __init__(self, backends: Iterable[object] | None = None) -> None:
        if backends is None:
            backends = [_KeychainBackend(), _EncryptedFileBackend(), _EnvBackend()]
        self.backends = [b for b in backends if getattr(b, "available", True)]

    def get(self, name: str, default: str | None = None) -> str | None:
        for b in self.backends:
            value = b.get(name)  # type: ignore[attr-defined]
            if value:
                return value
        return default

    def require(self, name: str) -> str:
        value = self.get(name)
        if not value:
            raise SecretNotFound(name)
        return value

    def set(self, name: str, value: str) -> str:
        for b in self.backends:
            if b.name in ("os-keychain", "encrypted-file"):  # type: ignore[attr-defined]
                b.set(name, value)  # type: ignore[attr-defined]
                return b.name  # type: ignore[attr-defined]
        self.backends[-1].set(name, value)  # type: ignore[attr-defined]
        return self.backends[-1].name  # type: ignore[attr-defined]

    def delete(self, name: str) -> None:
        """Remove a credential from every backend. This is the 'one command'."""
        for b in self.backends:
            b.delete(name)  # type: ignore[attr-defined]

    def backend_for(self, name: str) -> str:
        for b in self.backends:
            if b.get(name):  # type: ignore[attr-defined]
                return b.name  # type: ignore[attr-defined]
        return "-"

    def describe(self) -> list[SecretStatus]:
        out: list[SecretStatus] = []
        for name, (unlocks, where) in KNOWN_SECRETS.items():
            present = self.get(name) is not None
            out.append(
                SecretStatus(name, present, self.backend_for(name) if present else "-", unlocks, where)
            )
        return out


_VAULT: Vault | None = None


def vault() -> Vault:
    global _VAULT
    if _VAULT is None:
        _VAULT = Vault()
    return _VAULT


def get_secret(name: str, default: str | None = None) -> str | None:
    """The single interface every module uses. Never log the return value."""
    return vault().get(name, default)


def require_secret(name: str) -> str:
    return vault().require(name)


def has_secret(name: str) -> bool:
    return vault().get(name) is not None


def redact(text: str) -> str:
    """Scrub any known secret value out of a string before it is logged."""
    v = vault()
    for name in KNOWN_SECRETS:
        value = v.get(name)
        if value and len(value) >= 6 and value in text:
            text = text.replace(value, f"<{name}:redacted>")
    return text
