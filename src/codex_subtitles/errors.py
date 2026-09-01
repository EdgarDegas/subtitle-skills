from __future__ import annotations


class WorkflowError(RuntimeError):
    """A user-facing workflow failure."""


class ValidationIssue(WorkflowError):
    """A named validation failure, optionally localizable to target cue IDs."""

    def __init__(
        self,
        check: str,
        message: str,
        *,
        cue_ids: tuple[int, ...] = (),
        skippable: bool = True,
    ) -> None:
        super().__init__(message)
        self.check = check
        self.cue_ids = cue_ids
        self.skippable = skippable
