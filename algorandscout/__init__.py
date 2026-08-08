# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""
Algorandscout — a Blockscout-shaped read API for Algorand.

Independent work. Contains no Blockscout source code; interoperates by response shape
only. See NOTICE for the separability statement that this licence depends on.
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
    "capabilities",
    "get_capabilities",
    "__version__",
]
