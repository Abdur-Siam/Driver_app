"""Production WSGI entrypoint for the Driver App.

    cd Driver/app && gunicorn -c gunicorn.conf.py wsgi:app

Importable from any CWD. All configuration is environment-driven — see
backend/config.py and the commercial deploy runbook.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.server import app  # noqa: E402,F401
