"""`python -m minicrawl.web` — the browser front end."""
import argparse
import asyncio

from .server import serve


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="minicrawl web ui")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    try:
        asyncio.run(serve(args.host, args.port, not args.no_browser))
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
