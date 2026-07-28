"""Convenience launcher for the standalone Driver App.

    python Driver/app/serve.py        # http://127.0.0.1:5179

Works from any CWD (config paths are resolved from this file's location).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.server import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("DRIVER_APP_PORT", "5179"))
    host = os.environ.get("DRIVER_APP_HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=False)
