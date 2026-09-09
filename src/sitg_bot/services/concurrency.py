class StaleWriteError(ValueError):
    """Raised when a mutation targets an older version of a mutable aggregate."""
