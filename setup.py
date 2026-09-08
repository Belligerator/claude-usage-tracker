"""py2app build config for the menu bar app.

    pip3 install py2app
    python3 setup.py py2app

Produces dist/Claude Usage Tracker.app.
"""
from setuptools import setup

APP = ["claude_monitor.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "Claude Usage Tracker",
        "CFBundleDisplayName": "Claude Usage Tracker",
        "CFBundleIdentifier": "com.dima.claudeusagetracker",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "LSUIElement": True,
        "NSHumanReadableCopyright": "",
    },
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
