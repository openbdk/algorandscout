# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""
Algorandscout — an explorer API for Algorand.

Accounts, assets, applications, transactions and rounds, served over algod + indexer.
Part of the Open Blockchain Development Kit, licensed under the BANKON License.
An independent work: see NOTICE.
"""

from . import capabilities  # submodule — must stay bound to the module, not a function
from .capabilities import MODULE_VERSION
from .capabilities import capabilities as get_capabilities
from .client import AlgorandClient, AlgorandConfig, AlgorandError, NotFound

__version__ = MODULE_VERSION
__license__ = "Apache-2.0 (BANKON License)"
__all__ = [
    "AlgorandClient",
    "AlgorandConfig",
    "AlgorandError",
    "NotFound",
    "__version__",
    "capabilities",
    "get_capabilities",
]
