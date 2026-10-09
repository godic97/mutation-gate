#!/bin/sh
# Install the toolchains the integration tests drive (Stryker + vitest, mutmut + pytest).
set -e
here="$(cd "$(dirname "$0")" && pwd)"
(cd "$here/fixtures/js-mini" && npm install --silent --no-audit --no-fund)
(cd "$here/fixtures/js-jest-mini" && npm install --silent --no-audit --no-fund)
uv venv -q "$here/fixtures/py-mini/.venv"
uv pip install -q --python "$here/fixtures/py-mini/.venv/bin/python" -r "$here/fixtures/py-mini/requirements.txt"
