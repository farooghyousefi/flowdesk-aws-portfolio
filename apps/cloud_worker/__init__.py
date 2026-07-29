"""Flowdesk cloud worker.

The worker is intentionally separate from the localhost application. It runs
one bounded cloud job, writes verified artifacts to S3, and then exits.
"""

WORKER_VERSION = "0.2.4"
