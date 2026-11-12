"""Persist Linux file replacements before reporting success; preserve the macOS path."""
import os
import sys


def sync_file(output):
    if sys.platform == 'linux':
        output.flush()
        os.fsync(output.fileno())


def sync_directory(directory):
    if sys.platform == 'linux':
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
