"""Entry point for the embedded desktop S/R terminal.

    python run_desktop.py            # launch the app
    python run_desktop.py --selftest # headless: validate the whole pipeline, exit 0/1
    python run_desktop.py --version  # print version

Packaged as an .exe via PyInstaller (see packaging/desktop.spec). The same
flags work on the built binary, e.g.  SR-Terminal.exe --selftest
"""
import sys


def _main():
    if "--version" in sys.argv:
        import sr
        print(f"SR Terminal {sr.__version__}")
        return 0
    if "--selftest" in sys.argv:
        from sr.desktop import selftest
        return 0 if selftest() else 1
    from sr.desktop import main
    main()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
