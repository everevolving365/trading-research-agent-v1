"""Every failure the agent raises on purpose."""
from __future__ import annotations


class EEError(Exception):
    """Base class."""


class SpecError(EEError):
    """The strategy spec is invalid, ambiguous or unapprovable."""


class PrimitiveError(EEError):
    """A condition primitive is missing an implementation for some target."""


class DataError(EEError):
    """Data could not be fetched, or failed integrity checks."""


class CostModelMissing(EEError):
    """A backtest was attempted without a cost model. Hard rule 7."""


class LookaheadDetected(EEError):
    """The strategy peeked at information it could not have had."""


class HedgeRefused(EEError):
    """An order would have created opposing exposure across accounts. Hard rule 3."""


class KillSwitchTripped(EEError):
    """A kill switch halted execution."""


class SandboxViolation(EEError):
    """Generated code tried to leave the sandbox."""


class AutonomyViolation(EEError):
    """The action exceeds the current autonomy level."""


class OperatorRefusal(EEError):
    """The Operator was asked to do something it is forbidden to do."""
