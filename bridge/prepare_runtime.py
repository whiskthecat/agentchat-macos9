#!/usr/bin/env python3
"""Install the bundled Mac operating workspace without changing user config."""
import argparse
import json
from mac_context import prepare_runtime

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="Persistent agent working directory; defaults to AGENT_WORKSPACE or the user data directory")
    args = parser.parse_args()
    print(json.dumps(prepare_runtime(args.workspace), indent=2))
