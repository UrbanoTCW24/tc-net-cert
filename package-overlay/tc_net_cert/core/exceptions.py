"""Errors the operator can act on. Unexpected bugs still propagate."""


class ToolError(Exception):
    """Expected operational failure."""


class ConfigError(ToolError):
    """Configuration is missing, duplicated, or inconsistent."""


class SafetyError(ToolError):
    """An action would touch the management plane or leave SAFE MODE."""


class MissingToolError(ToolError):
    """A required external binary is not installed."""

    def __init__(self, tool: str, hint: str) -> None:
        self.tool = tool
        self.hint = hint
        super().__init__(f"Required tool '{tool}' is not installed. {hint}")


class PreflightError(ToolError):
    """The station is not ready to move interfaces or start a run."""


class TestFailure(ToolError):
    """A certification measurement failed. Evidence is still reported."""
