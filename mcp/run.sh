#!/bin/sh
# Python 3.10 以上で studio の MCP サーバーを起動する（macOS の /usr/bin/python3 は 3.9）。
PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:$PATH"
export PATH
DIR="$(cd "$(dirname "$0")" && pwd)"
for py in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    exec "$py" "$DIR/studio_server.py"
  fi
done
echo "threads-video-studio: Python 3.10 以上が必要（brew install python で入れられる）" >&2
exit 1
