"""Compile nested Blueprint resources and expose a real output file to Ninja."""

from pathlib import Path
import subprocess
import sys

compiler, output_directory, source_directory, stamp, *inputs = sys.argv[1:]
subprocess.run([compiler, 'batch-compile', output_directory, source_directory, *inputs], check=True)
Path(stamp).touch()
