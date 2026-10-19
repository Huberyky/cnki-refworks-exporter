"""Compatibility entry point; use cnki_refworks_exporter.py for new commands."""
from cnki_refworks_exporter import *  # Keep existing imports and commands working.

if __name__ == "__main__":
    main()
