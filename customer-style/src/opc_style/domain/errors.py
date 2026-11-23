"""Errors raised when a request violates a style-domain rule."""


class DomainRuleError(Exception):
    """A stable, machine-readable domain-rule failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
