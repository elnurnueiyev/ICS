"""Entry point: python3 run.py [--host 127.0.0.1 --port 8000 --db PATH]"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ics_platform.server import serve  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="ICS/OT Security Academy")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=None, help="SQLite path (default: ics_platform.db)")
    args = ap.parse_args()
    port = args.port
    try:
        serve(host=args.host, port=port, db_path=args.db)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
