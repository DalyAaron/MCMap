"""Start the MCMap web app: python -m mcmap"""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

from .arnis import ARNIS_VERSION
from .config import Config
from .server import App, make_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcmap", description="Select an area on a map and turn it into a Minecraft world.")
    parser.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port to listen on (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser on start")
    parser.add_argument("--arnis", type=Path, help=f"use this Arnis executable instead of downloading {ARNIS_VERSION}")
    parser.add_argument("--saves-dir", type=Path, help="Minecraft Java saves folder to install worlds into")
    parser.add_argument("--data-dir", type=Path, help="where MCMap keeps Arnis, generated worlds and job history")
    args = parser.parse_args()

    config = Config()
    if args.data_dir:
        config.data_dir = args.data_dir.expanduser().resolve()
    if args.arnis:
        config.arnis_path = args.arnis.expanduser().resolve()
    if args.saves_dir:
        config.saves_dir = args.saves_dir.expanduser().resolve()

    app = App(config)
    server = make_server(app, args.host, args.port)
    shown_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0", "::") else args.host
    url = f"http://{shown_host}:{args.port}/"

    arnis = app.installer.binary_path()
    print(f"MCMap running at {url}")
    print(f"  Arnis:  {arnis or f'not installed yet (downloads {ARNIS_VERSION} on first use)'}")
    print(f"  Worlds: {config.saves_dir or config.worlds_dir}")
    print("Press Ctrl+C to stop.")

    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        app.jobs.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
