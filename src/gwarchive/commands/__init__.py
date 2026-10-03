"""Importing this package registers all 19 commands on the Typer app.

THE IMPORT ORDER BELOW IS THE --help ORDER. Each module registers its commands
with @app.command() at import time, so reordering these lines reorders the help
page. That is why this block carries a per-file ignore for I001 in .ruff.toml:
ruff's isort would otherwise merge and alphabetise them on the next
`ruff check --fix`, silently rewriting the help page. The order is pinned by
test_every_command_is_registered_in_help_order so a reorder fails loudly rather
than drifting.

oldify sits with the other relocation verbs rather than after find, which is
where it used to be. mv/rename/cp/oldify are one family and read better
together; nothing depends on the old position.
"""

from gwarchive.commands import structure  # noqa: F401  init, create, mksub
from gwarchive.commands import move  # noqa: F401  mv, rename, cp, oldify
from gwarchive.commands import browse  # noqa: F401  list, find
from gwarchive.commands import upload  # noqa: F401  push, offload
from gwarchive.commands import download  # noqa: F401  pull, restore
from gwarchive.commands import stats  # noqa: F401
from gwarchive.commands import verify  # noqa: F401
from gwarchive.commands import shell  # noqa: F401  clears, cd, here, shell-init
