# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""Run the module as a service: ``python -m algorandscout``."""

from __future__ import annotations

import argparse
import logging
import os


def main() -> None:
    parser = argparse.ArgumentParser(prog="algorandscout", description="Blockscout-shaped read API for Algorand")
    parser.add_argument("--host", default=os.environ.get("OPENBDK_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("OPENBDK_PORT", "8100")))
    parser.add_argument("--log-level", default=os.environ.get("OPENBDK_LOG_LEVEL", "info"))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - install-time guidance
        raise SystemExit("the service extra is required: pip install 'algorandscout[service]'") from exc

    uvicorn.run(
        "algorandscout.service:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
