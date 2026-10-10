#!/bin/sh
# Start the studio MCP server with a Python >= 3.10 (macOS ships 3.9 at /usr/bin/python3).
PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:$PATH"
export PATH
DIR="$(cd "$(dirname "$0")" && pwd)"
for py in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    exec "$py" "$DIR/studio_server.py"
  fi
done
echo "threads-video-studio: Python 3.10 or newer is required (install with: brew install python)" >&2
exit 1
