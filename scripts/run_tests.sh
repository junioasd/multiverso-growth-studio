#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -W error::ResourceWarning -m unittest discover -s tests -v
