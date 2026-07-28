"""Gunicorn config for the Driver App (production).

    cd Driver/app && gunicorn -c gunicorn.conf.py wsgi:app

Access + error logs go to stdout — the accepted TOM ops pattern (gunicorn
stdout → App Service log stream → Log Analytics, 90-day retention).

The app is multi-worker safe: abuse-control state and the push outbox live
in the database (WAL SQLite), not in process memory. Workers share one host
so the SQLite file is shared; busy_timeout=10s absorbs write contention.
"""
import multiprocessing
import os

bind = "0.0.0.0:" + os.environ.get("DRIVER_APP_PORT", os.environ.get("PORT", "8000"))

# Modest by default: the fleet is tens of drivers, not thousands. Threads
# cover the slow bits (media upload decode, outbound FCM/Maps calls).
workers = int(os.environ.get("WEB_CONCURRENCY", min(4, multiprocessing.cpu_count() + 1)))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))

# POD photo uploads from a phone on 3G can be slow; don't kill them mid-body.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

# Cap memory creep on long-lived workers.
max_requests = 2000
max_requests_jitter = 200

accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms "%(a)s"'
