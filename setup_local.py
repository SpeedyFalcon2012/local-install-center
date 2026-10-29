#!/usr/bin/env python3
"""Initial no-sudo setup for Local Install Center."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tkinter as tk
from tkinter import messagebox


APP_ID = "local-install-center"
APP_TITLE = "Local Install Center"
APP_HOME = Path.home() / ".local" / "share" / APP_ID
RELEASES_HOME = APP_HOME / "releases"
APPLICATIONS_HOME = Path.home() / ".local" / "share" / "applications"
SOURCE_HOME = Path(__file__).resolve().parent


def desktop_quote(value: str) -> str:
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("$", "\\$").replace("`", "\\`")
    return f'"{value}"'


def write_launcher(version: str, release: Path) -> None:
    APPLICATIONS_HOME.mkdir(parents=True, exist_ok=True)
    target = APPLICATIONS_HOME / f"{APP_ID}.desktop"
    app_script = release / "install_center.py"
    icon = release / "assets" / "local-install-center.png"
    content = (
        "[Desktop Entry]\nType=Application\n"
        f"Name={APP_TITLE}\n"
        f"Exec={desktop_quote(sys.executable)} {desktop_quote(str(app_script))}\n"
        f"Icon={icon}\n"
        "Terminal=false\nCategories=Utility;System;\nStartupNotify=true\n"
        f"Comment=Install and manage user-level Linux apps (v{version})\n"
    )
    temporary = target.with_suffix(".desktop.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, target)


def show_message(kind: str, text: str) -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        if kind == "error":
            messagebox.showerror(APP_TITLE, text)
        else:
            messagebox.showinfo(APP_TITLE, text)
        root.destroy()
    except tk.TclError:
        print(text, file=sys.stderr if kind == "error" else sys.stdout)


def main() -> int:
    manifest_path = SOURCE_HOME / "release.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = str(manifest["version"])
    if manifest.get("application") != APP_ID:
        raise ValueError("This folder is not a Local Install Center release.")
    source_script = SOURCE_HOME / "install_center.py"
    source_icon = SOURCE_HOME / "assets" / "local-install-center.png"
    if not source_script.is_file() or not source_icon.is_file():
        raise FileNotFoundError("The app source or icon is missing from this release folder.")

    RELEASES_HOME.mkdir(parents=True, exist_ok=True)
    release = RELEASES_HOME / version
    if not release.exists():
        staging = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=RELEASES_HOME))
        try:
            (staging / "assets").mkdir()
            shutil.copy2(source_script, staging / "install_center.py")
            shutil.copy2(manifest_path, staging / "release.json")
            shutil.copy2(source_icon, staging / "assets" / "local-install-center.png")
            os.replace(staging, release)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    write_launcher(version, release)
    show_message("info", f"Setup is complete. Local Install Center {version} is in your application menu.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        show_message("error", f"Setup failed: {exc}")
        raise SystemExit(1)
