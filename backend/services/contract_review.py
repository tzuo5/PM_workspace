# -*- coding: utf-8 -*-
"""Compatibility entry point for the contract review service.

The implementation is split into a deterministic engine and a maintainable
knowledge loader.  Existing imports from ``services.contract_review`` remain
stable for the server, browser API, and tests.
"""

from services.contract_review_engine import *  # noqa: F401,F403
