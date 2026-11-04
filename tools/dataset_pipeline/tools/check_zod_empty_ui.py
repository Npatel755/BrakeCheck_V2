"""Compatibility entry point for the current one-action/empty-label UI checks."""
from pathlib import Path
from runpy import run_path

run_path(str(Path(__file__).with_name('check_zod_single_action_ui.py')), run_name='__main__')
