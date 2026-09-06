"""Allow running as `python -m istota.skills.browse`.

`sys.exit(main())` rather than a bare call, so a `main` that returns an exit
code (`ntfy`) has it passed on; identical for every `main` that exits itself.
One shape for all of them — `tests/test_skill_cli_facade.py` is the pin.
"""

import sys

from . import main

sys.exit(main())
