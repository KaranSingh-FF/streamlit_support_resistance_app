"""Entry point for the S/R terminal (local server + browser).

    python run_desktop.py             # start the server and open the browser
    python run_desktop.py --port 8765 # use a fixed port (don't auto-pick)
    python run_desktop.py --selftest  # headless: validate engine + HTTP routes, exit 0/1
    python run_desktop.py --version

Packaged as an .exe via PyInstaller (packaging/desktop.spec). The same flags work
on the built binary, e.g.  SR-Terminal.exe --selftest
"""
import sys


def _ensure_streams():
    """A windowed (console=False) PyInstaller build has sys.stdout/stderr == None, so
    any print() would raise and crash the app at launch. Point them at a per-session
    log file beside the data store (truncated each run) so output has somewhere to go
    and startup failures stay diagnosable."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        from sr.desktop import _resolve_data_dir
        log_dir = _resolve_data_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        sink = open(log_dir / "sr_terminal.log", "w", buffering=1, encoding="utf-8")
    except Exception:
        import os
        sink = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink


def _main():
    _ensure_streams()
    args = sys.argv[1:]
    if "--version" in args:
        import sr
        print(f"SR Terminal {sr.__version__}")
        return 0
    if "--selftest" in args:
        from sr.desktop import selftest
        return 0 if selftest() else 1
    if "--feed" in args:   # the supervised live-feed subprocess (spawned by the app)
        i = args.index("--feed")
        level = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else "l1"
        from sr.live.feed_proc import run_feed_l1
        return run_feed_l1(level)
    if "--no-feed" in args:   # analysis-only this launch (no live feed / alerts)
        import os
        os.environ["SR_NO_FEED"] = "1"
    from sr.desktop import main
    port = None
    if "--port" in args:
        try:
            port = int(args[args.index("--port") + 1])
            if not (1 <= port <= 65535):
                print("--port must be 1-65535; auto-selecting a free port instead.", file=sys.stderr)
                port = None
        except (IndexError, ValueError):
            print("--port requires an integer; auto-selecting a free port instead.", file=sys.stderr)
            port = None
    main(port=port)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
