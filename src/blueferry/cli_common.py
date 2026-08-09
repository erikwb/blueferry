"""Small presentation helpers shared by CLI command modules."""
from __future__ import annotations

import logging
import sys


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        stream=sys.stderr,
    )
