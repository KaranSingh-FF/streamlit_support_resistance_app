"""Entry point for the embedded desktop S/R terminal.

    python run_desktop.py

Packaged as an .exe via PyInstaller (see build/desktop.spec).
"""
from sr.desktop import main

if __name__ == "__main__":
    main()
