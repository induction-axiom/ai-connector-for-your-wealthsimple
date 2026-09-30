#!/bin/sh
# One command from an empty Google Cloud project to a working connector.
# Safe to re-run: it reuses what already exists, so re-running is also how you update.
# Usage: firebase/scripts/bootstrap.sh [PROJECT_ID [OWNER_EMAIL]]
# Without them, it uses the selected gcloud project and the signed-in gcloud account.
# This only finds a Python to run the setup; manage.py does the rest and shows each step.
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)

for python in python3.13 python3; do
  if command -v "$python" >/dev/null 2>&1 \
      && "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    exec "$python" "$repo_root/firebase/scripts/manage.py" bootstrap \
      ${1:+--project "$1"} ${2:+--owner-email "$2"}
  fi
done

# No Python new enough to run the setup: install the one the functions need anyway.
echo "Installing Python 3.13 for the deploy tools (one time)..."
curl -LsSf https://astral.sh/uv/0.12.18/install.sh | sh -s -- --quiet
"$HOME/.local/bin/uv" python install 3.13 --quiet
python=$("$HOME/.local/bin/uv" python find 3.13)
exec "$python" "$repo_root/firebase/scripts/manage.py" bootstrap \
  ${1:+--project "$1"} ${2:+--owner-email "$2"} --python "$python"
