#!/usr/bin/env python3
"""Cron entry point for the task-owned device-roadmap rollover."""
from pathlib import Path
import runpy

runpy.run_path(
    "/home/hermes/tasks/_tools/roll_forward_device_roadmap.py",
    run_name="__main__",
)
