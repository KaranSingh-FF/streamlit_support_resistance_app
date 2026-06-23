"""Entry point for the S/R terminal (local server + browser).

    python run_desktop.py             # start the server and open the browser
    python run_desktop.py --port 8765 # use a fixed port (don't auto-pick)
    python run_desktop.py --selftest  # headless: validate engine + HTTP routes, exit 0/1
    python run_desktop.py --version

Packaged as an .exe via PyInstaller (packaging/desktop.spec). The same flags work
on the built binary, e.g.  SR-Terminal.exe --selftest
"""
import sys


def _main():
    args = sys.argv[1:]
    if "--version" in args:
        import sr
        print(f"SR Terminal {sr.__version__}")
        return 0
    if "--selftest" in args:
        from sr.desktop import selftest
        return 0 if selftest() else 1
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
