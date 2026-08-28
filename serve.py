#!/usr/bin/env python3
"""Serve a crawled folder over http:// -- some pages behave better than file://."""
from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
import sys
import webbrowser
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a crawled site locally.")
    parser.add_argument("directory", nargs="?", default="output",
                        help="folder to serve (default: ./output)")
    parser.add_argument("-p", "--port", type=int, default=8800)
    parser.add_argument("--no-open", action="store_true", help="don't open a browser")
    args = parser.parse_args()

    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", args.port), handler) as server:
        url = f"http://127.0.0.1:{args.port}/"
        print(f"serving {root}\n  -> {url}\nCtrl-C to stop")
        if not args.no_open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
