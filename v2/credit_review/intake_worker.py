"""Run a separate intake worker using the same release as the web application."""
import argparse
import logging
from time import sleep

from .api import MAPPER, parse_pdf
from .intake_jobs import run_one
from .object_storage import enabled


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Process at most one queued statement")
    args = parser.parse_args()
    if not enabled():
        parser.error("Set CREDIT_UPLOAD_BUCKET before running an intake worker")
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            processed = run_one(parse_pdf, MAPPER)
        except Exception:
            logging.error("intake_worker_database_unavailable")
            if args.once:
                raise SystemExit(1)
            sleep(5)
            continue
        if args.once:
            break
        if not processed:
            sleep(2)


if __name__ == "__main__":
    main()
