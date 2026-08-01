"""Data quality.

`checks` holds CAUSAL checks -- each looks only at bar `t` and bars before it, so its
output is safe to use as a feature.

`offline` holds NON-CAUSAL checks, which read bar `t+1`. They are correct for an
offline QA report and catastrophic if they ever leak into a feature. They live in a
separate module rather than behind a boolean flag because a flag eventually gets
ignored during a refactor, whereas an import boundary is visible.
"""

from . import checks, offline, report

__all__ = ["checks", "offline", "report"]
