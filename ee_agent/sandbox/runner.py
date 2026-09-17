"""Law Three: nothing untrusted runs unwatched.

Generated code is displayed before it executes, and then executes in a separate
process with:

* a stripped environment -- no API keys reach the child (hard rule 8),
* no network -- sockets are disabled inside the child before user code loads,
* a restricted write path -- one scratch directory, nothing else,
* a hard timeout,
* and a captured transcript of everything it printed.

The review gate is not optional: :func:`run` refuses to execute code that has
not been approved, and ``approve=True`` must be passed by a caller that has
actually shown the source to a human (or, in tests, by a fixture approver).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from ee_agent.errors import SandboxViolation
from ee_agent.secrets.vault import KNOWN_SECRETS

#: Environment variables the child is allowed to see. Everything else is dropped.
ENV_ALLOWLIST = {"PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONHASHSEED", "TZ"}

PREAMBLE = '''
# ---- sandbox preamble: injected by ee_agent.sandbox, not by the strategy ----
import builtins, os, socket, sys

_ALLOWED_WRITE_ROOT = os.environ.get("EE_SANDBOX_SCRATCH", "")

def _no_network(*args, **kwargs):
    raise RuntimeError("sandbox: network access is disabled")

socket.socket = _no_network
socket.create_connection = _no_network
socket.getaddrinfo = _no_network
try:
    import ssl
    ssl.SSLContext.wrap_socket = _no_network
except Exception:
    pass

_real_open = builtins.open

def _guarded_open(file, mode="r", *args, **kwargs):
    path = os.path.abspath(str(file))
    writing = any(flag in mode for flag in ("w", "a", "x", "+"))
    if writing and not path.startswith(os.path.abspath(_ALLOWED_WRITE_ROOT)):
        raise PermissionError(f"sandbox: writes are confined to {_ALLOWED_WRITE_ROOT}")
    return _real_open(file, mode, *args, **kwargs)

builtins.open = _guarded_open

for _leaked in [k for k in os.environ if k.endswith(("_KEY", "_SECRET", "_TOKEN", "_PASSWORD"))]:
    os.environ.pop(_leaked, None)
# ---- end sandbox preamble --------------------------------------------------
'''


@dataclass
class SandboxResult:
    ok: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_s: float = 0.0
    scratch_dir: str = ""
    artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "duration_s": round(self.duration_s, 3),
            "stdout_tail": self.stdout[-4000:],
            "stderr_tail": self.stderr[-4000:],
            "artifacts": self.artifacts,
        }


def review_banner(source: str, title: str = "generated code") -> str:
    """What the client sees before anything runs. Law Three, first clause."""
    lines = source.splitlines()
    numbered = "\n".join(f"  {i + 1:>4} | {line}" for i, line in enumerate(lines))
    return (
        f"\n{'=' * 76}\n  REVIEW GATE -- {title} ({len(lines)} lines)\n"
        f"  This has not run yet. It will run with no network, no API keys, and a "
        f"single writable directory.\n{'=' * 76}\n{numbered}\n{'=' * 76}\n"
    )


def run(
    source: str,
    *,
    approve: bool,
    timeout_s: float = 120.0,
    scratch: Path | None = None,
    argv: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> SandboxResult:
    """Execute generated Python in a stripped subprocess.

    ``approve`` must be True and must come from a caller that displayed the
    source. There is no default-approve path.
    """
    if not approve:
        raise SandboxViolation(
            "Generated code was not approved for execution. Display it with review_banner() "
            "and obtain an explicit approval first (Law Three)."
        )

    import time

    scratch_dir = Path(scratch) if scratch else Path(tempfile.mkdtemp(prefix="ee-sandbox-"))
    scratch_dir.mkdir(parents=True, exist_ok=True)
    script = scratch_dir / "generated.py"
    script.write_text(PREAMBLE + "\n" + textwrap.dedent(source), encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
    for secret in KNOWN_SECRETS:
        env.pop(secret, None)
    env["EE_SANDBOX_SCRATCH"] = str(scratch_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"  # determinism
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    if extra_env:
        for key, value in extra_env.items():
            if key in KNOWN_SECRETS:
                raise SandboxViolation(f"refusing to pass secret {key} into the sandbox (hard rule 8)")
            env[key] = value

    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-S", str(script), *(argv or [])],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            cwd=str(scratch_dir),
        )
        duration = time.time() - started
        return SandboxResult(
            ok=proc.returncode == 0,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_s=duration,
            scratch_dir=str(scratch_dir),
            artifacts=[str(p.name) for p in scratch_dir.iterdir() if p.name != "generated.py"],
        )
    except subprocess.TimeoutExpired as exc:
        return SandboxResult(
            ok=False,
            returncode=-1,
            stdout=(exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr=f"sandbox: timed out after {timeout_s}s",
            timed_out=True,
            duration_s=time.time() - started,
            scratch_dir=str(scratch_dir),
        )


def self_test() -> dict:
    """Proves the sandbox holds. Used by the test suite and by `make verify`."""
    probes = {
        "network": "import socket; socket.create_connection(('example.com', 80))",
        "secret": (
            "import os; "
            "print('LEAK' if any(k.endswith('_KEY') for k in os.environ) else 'no-secrets')"
        ),
        "write_outside": "open('/etc/ee-agent-probe', 'w').write('x')",
        "write_inside": "open('ok.txt', 'w').write('x'); print('wrote')",
    }
    out = {}
    for name, code in probes.items():
        result = run(code, approve=True, timeout_s=30)
        out[name] = {"ok": result.ok, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()[-200:]}
    return out
