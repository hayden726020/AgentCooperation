"""Project-level pytest configuration.

Ensures Windows temporary directories use a project-local .pytest_temp directory
to prevent Windows Temp folder permissions or locking conflicts.
"""

import os
import tempfile
from pathlib import Path

_LOCAL_TEMP = Path(__file__).resolve().parent / ".pytest_temp"
_LOCAL_TEMP.mkdir(exist_ok=True)
os.environ["TEMP"] = str(_LOCAL_TEMP)
os.environ["TMP"] = str(_LOCAL_TEMP)
tempfile.tempdir = str(_LOCAL_TEMP)
