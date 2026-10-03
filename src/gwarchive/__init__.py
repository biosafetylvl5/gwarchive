"""gwarchive -- organize files under the GWArchive naming standard.

Categories are P/R/M/A/O -> Project/Recurring/Material/Archive/Old.

A prefix (``P0001``) is a permanent identifier. It is allocated once, it is
never reissued, and it travels with the folder across category moves -- so a
folder created in Project keeps its ``P`` prefix even after it is moved into
Archive. A *copy* is a new thing and gets a fresh identifier.

This package is being carved out of the original single-file ``g.py``. The
module layering, and the two rules that keep it honest, are documented in
AGENTS.md; ``tests/test_structure.py`` enforces them.
"""

# Kept on one line with a plain literal: `cz bump` rewrites it by anchored
# textual replace (see [tool.commitizen].version_files), and [project].version
# in pyproject.toml is the authority it is synced against.
#
# g.py still carries its own copy until the strangle reaches it; the two are
# reconciled when g.py starts importing from this package.
__version__ = "0.3.0"
