#!/usr/bin/env python3
"""Thin entry-point shim for the forgewright package.

Allows invocations like
    python forgewright.py
    python /opt/forgewright/forgewright.py
to delegate to forgewright.main without needing the installed script.
"""

import sys

from forgewright.main import main

sys.exit(main())
