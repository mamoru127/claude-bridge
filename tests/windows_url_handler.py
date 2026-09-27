"""Disposable URL handler used by the native Windows CI test."""

import sys
from pathlib import Path


Path(sys.argv[2]).write_text(sys.argv[1], encoding="utf-8")
