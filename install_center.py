#!/usr/bin/env python3
"""Local Install Center: install and manage selected user-level Linux apps."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "Local Install Center"
APP_ID = "local-install-center"
APP_VERSION = "0.4.0"
APP_HOME = Path.home() / ".local" / "share" / APP_ID
RELEASES_HOME = APP_HOME / "releases"
APPIMAGE_HOME = Path.home() / ".local" / "opt" / APP_ID
APPLICATIONS_HOME = Path.home() / ".local" / "share" / "applications"
ICON_RELATIVE = Path("assets") / "local-install-center.png"
FLATHUB_REPO_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"
MAX_APPIMAGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_UPDATE_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UPDATE_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_UPDATE_FILES = 250


def desktop_quote(value: str) -> str:
    """Quote one Desktop Entry Exec argument (not a shell command)."""
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("$", "\\$").replace("`", "\\`")
    return f'"{value}"'


def valid_https_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username


def version_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", value):
        raise ValueError("Release version must use numbers such as 0.4.0.")
    return tuple(int(part) for part in value.split("."))


class ScrollablePage(ttk.Frame):
    """A notebook page that keeps its contents reachable in a short window."""

    def __init__(self, parent: tk.Misc, **kwargs: object) -> None:
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#f3f5f8")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.content = ttk.Frame(self.canvas, padding=(16, 14, 16, 18))
        self.window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.content.bind("<Configure>", self._update_scrollregion)
        self.canvas.bind("<Configure>", self._fit_content_width)
        self.canvas.bind_all("<MouseWheel>", self._mousewheel)
        self.canvas.bind_all("<Button-4>", self._mousewheel_linux)
        self.canvas.bind_all("<Button-5>", self._mousewheel_linux)

    def _update_scrollregion(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _fit_content_width(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def _mousewheel(self, event: tk.Event) -> None:
        if self.winfo_ismapped():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _mousewheel_linux(self, event: tk.Event) -> None:
        if self.winfo_ismapped():
            self.canvas.yview_scroll(-1 if event.num == 4 else 1, "units")


class InstallCenter(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} {APP_VERSION}")
        self.geometry("850x720")
        self.minsize(650, 500)
        self.configure(bg="#f3f5f8")
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.busy = False
        self.icon_image: tk.PhotoImage | None = None
        self._set_window_icon()
        self._build_ui()
        self.after(100, self._drain_events)
        self._refresh_tool_status()
        self._refresh_installed()

    def _set_window_icon(self) -> None:
        icon_path = Path(__file__).resolve().parent / ICON_RELATIVE
        if icon_path.is_file():
            try:
                image = tk.PhotoImage(file=str(icon_path))
                factor = max(1, image.width() // 64)
                self.icon_image = image.subsample(factor, factor)
                self.iconphoto(True, self.icon_image)
            except tk.TclError:
                pass

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background="#f3f5f8")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f3f5f8", foreground="#17202c", font=("Sans", 10))
        style.configure("Title.TLabel", font=("Sans", 19, "bold"), background="#f3f5f8")
        style.configure("Subtitle.TLabel", foreground="#526170", background="#f3f5f8")
        style.configure("Card.TLabel", background="#ffffff")
        style.configure("TButton", padding=(10, 6))
        style.configure("Accent.TButton", padding=(14, 8), font=("Sans", 10, "bold"))

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        if self.icon_image is not None:
            ttk.Label(header, image=self.icon_image, background="#f3f5f8").pack(side="left", padx=(0, 12))
        titlebox = ttk.Frame(header)
        titlebox.pack(side="left", fill="x", expand=True)
        ttk.Label(titlebox, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(titlebox, text=f"Version {APP_VERSION} · User-level software installs", style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Button(header, text="Install update…", command=self._choose_update).pack(side="right", anchor="n")

        note = ttk.Frame(outer, style="Card.TFrame", padding=(11, 8))
        note.pack(fill="x", pady=(11, 10))
        ttk.Label(
            note,
            text="Installs run as your account. No sudo and no raw shell scripts. Only install software from sources you trust.",
            style="Card.TLabel", wraplength=760,
        ).pack(anchor="w")

        self.tabs = ttk.Notebook(outer)
        self.appimage_page = ScrollablePage(self.tabs)
        self.flatpak_page = ScrollablePage(self.tabs)
        self.cli_page = ScrollablePage(self.tabs)
        self.installed_page = ttk.Frame(self.tabs, padding=10)
        self.about_page = ScrollablePage(self.tabs)
        self.tabs.add(self.appimage_page, text="AppImage")
        self.tabs.add(self.flatpak_page, text="Flatpak")
        self.tabs.add(self.cli_page, text="CLI tools")
        self.tabs.add(self.installed_page, text="Installed apps")
        self.tabs.add(self.about_page, text="About & limits")
        self.tabs.pack(side="top", fill="both", expand=True)
        self._build_appimage(self.appimage_page.content)
        self._build_flatpak(self.flatpak_page.content)
        self._build_cli(self.cli_page.content)
        self._build_installed()
        self._build_about(self.about_page.content)

        activity = ttk.LabelFrame(outer, text="Activity", padding=6)
        activity.pack(side="bottom", fill="x", pady=(8, 0))
        self.log = tk.Text(
            activity, height=4, wrap="word", state="disabled", font=("Monospace", 9),
            background="#101820", foreground="#e8eef5", insertbackground="#ffffff",
            relief="flat", padx=8, pady=6,
        )
        self.log.pack(fill="x", expand=False)

        actionbar = ttk.Frame(outer)
        actionbar.pack(side="bottom", fill="x", pady=(8, 0))
        self.primary_action = ttk.Button(actionbar, text="Download and install", style="Accent.TButton", command=self._run_primary_action)
        self.primary_action.pack(side="left")
        self.primary_action_callback = None
        self.status = ttk.Label(actionbar, text="Ready", style="Subtitle.TLabel")
        self.status.pack(side="right")
        self.progress = ttk.Progressbar(actionbar, mode="indeterminate", length=100)
        self.tabs.bind("<<NotebookTabChanged>>", self._update_primary_action)
        self._update_primary_action()
        self._log("Ready. Choose a method. The install action stays at the bottom of the window.")

    def _section(self, parent: ttk.Frame, title: str, subtitle: str) -> None:
        ttk.Label(parent, text=title, font=("Sans", 14, "bold")).pack(anchor="w")
        ttk.Label(parent, text=subtitle, wraplength=730, foreground="#526170").pack(anchor="w", pady=(4, 12))

    def _entry(self, parent: ttk.Frame, label: str, variable: tk.StringVar) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=5)
        ttk.Label(row, text=label, width=31).pack(side="left", anchor="w")
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)

    def _build_appimage(self, parent: ttk.Frame) -> None:
        self._section(parent, "Install an AppImage", "Use a direct HTTPS download link from the publisher’s website or release page.")
        self.app_name = tk.StringVar()
        self.app_url = tk.StringVar()
        self.app_hash = tk.StringVar()
        self._entry(parent, "Application name", self.app_name)
        self._entry(parent, "Direct HTTPS URL", self.app_url)
        self._entry(parent, "SHA-256 (optional)", self.app_hash)
        ttk.Label(parent, text=f"Saved under {APPIMAGE_HOME}", foreground="#526170").pack(anchor="w", pady=(5, 10))
        ttk.Label(parent, text="AppImages can run code with your account’s permissions. Only use a publisher you trust.", wraplength=730, foreground="#526170").pack(anchor="w")

    def _build_flatpak(self, parent: ttk.Frame) -> None:
        self._section(parent, "Install a Flatpak for your account", "Enter the app ID and remote. For flathub, the official user remote is configured automatically if needed.")
        self.flatpak_id = tk.StringVar()
        self.flatpak_remote = tk.StringVar(value="flathub")
        self._entry(parent, "Application ID", self.flatpak_id)
        self._entry(parent, "Remote", self.flatpak_remote)
        self.flatpak_status = ttk.Label(parent, text="Checking Flatpak…", foreground="#526170")
        self.flatpak_status.pack(anchor="w", pady=(6, 12))
        ttk.Label(parent, text="Examples: org.inkscape.Inkscape · com.discordapp.Discord · org.turbowarp.TurboWarp", wraplength=730, foreground="#526170").pack(anchor="w")
        ttk.Label(parent, text="Installs to your account. Installing Flatpak itself or changing system remotes may need an administrator.", wraplength=730, foreground="#526170").pack(anchor="w", pady=(12, 0))

    def _build_cli(self, parent: ttk.Frame) -> None:
        self._section(parent, "Install a command-line package", "Uses a package tool already available on your system; this app won’t bootstrap one with sudo.")
        self.cli_tool = tk.StringVar(value="pipx")
        self.cli_package = tk.StringVar()
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="Package tool", width=31).pack(side="left")
        ttk.Combobox(row, textvariable=self.cli_tool, values=("pipx", "cargo", "npm"), state="readonly", width=18).pack(side="left")
        self._entry(parent, "Package name", self.cli_package)
        self.cli_status = ttk.Label(parent, text="Checking installed tools…", foreground="#526170")
        self.cli_status.pack(anchor="w", pady=(6, 12))
        ttk.Label(parent, text="Package build and install hooks may run code as your account. This is not a sandbox.", wraplength=730, foreground="#526170").pack(anchor="w")

    def _build_installed(self) -> None:
        header = ttk.Frame(self.installed_page)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="Your user-installed Flatpaks", font=("Sans", 14, "bold")).pack(side="left")
        ttk.Button(header, text="Refresh", command=self._refresh_installed).pack(side="right")
        columns = ("name", "application", "version", "remote")
        self.apps_tree = ttk.Treeview(self.installed_page, columns=columns, show="headings", height=7, selectmode="browse")
        for key, label, width in (("name", "App", 180), ("application", "App ID", 230), ("version", "Version", 90), ("remote", "Remote", 110)):
            self.apps_tree.heading(key, text=label)
            self.apps_tree.column(key, width=width, minwidth=65, stretch=(key in ("name", "application")))
        self.apps_tree.pack(fill="both", expand=True)
        actions = ttk.Frame(self.installed_page)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="Launch selected", command=self._launch_selected_app).pack(side="left")
        ttk.Button(actions, text="Uninstall selected…", command=self._uninstall_selected).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Update my Flatpaks", command=self._update_flatpaks).pack(side="right")
        ttk.Label(self.installed_page, text="Uninstall leaves the app’s settings and files in your home folder. You can remove those later from Nemo if you choose.", wraplength=720, foreground="#526170").pack(anchor="w", pady=(8, 0))

    def _build_about(self, parent: ttk.Frame) -> None:
        self._section(parent, "A small, user-level software installer", "Local Install Center gives a graphical path through a few well-supported Linux install formats.")
        lines = [
            ("AppImage", "Downloads a direct HTTPS AppImage, optionally checks a publisher-provided SHA-256, saves it under your account, and adds a menu launcher."),
            ("Flatpak", "Installs per-user. It can add the official Flathub remote for your account and lists, launches, updates, and uninstalls user Flatpaks."),
            ("pipx, Cargo, npm", "Installs a named CLI package when the selected tool is already installed. Package hooks still run as your account."),
            ("APT and .deb", "Not supported as no-admin installs: they write system-owned files and update the system package database."),
            ("curl | sh", "Not supported generically. Remote scripts can do arbitrary things, may ask for sudo, and are not reliably convertible to user-local installs."),
            ("Updates", "Choose a Local Install Center release archive. It is checked for expected files, paths, version metadata, and Python syntax before the menu launcher switches. Older installed releases are kept."),
        ]
        for title, detail in lines:
            card = ttk.Frame(parent, style="Card.TFrame", padding=10)
            card.pack(fill="x", pady=4)
            ttk.Label(card, text=title, style="Card.TLabel", font=("Sans", 10, "bold")).pack(anchor="w")
            ttk.Label(card, text=detail, style="Card.TLabel", wraplength=710).pack(anchor="w", pady=(3, 0))
        ttk.Label(parent, text="Local Install Center does not certify downloads as safe. Update archive checks catch malformed or inconsistent packages; they do not authenticate the publisher.", wraplength=730, foreground="#526170").pack(anchor="w", pady=(10, 0))

    def _update_primary_action(self, _event: tk.Event | None = None) -> None:
        selected = self.tabs.index(self.tabs.select())
        actions = {
            0: ("Download and install", self._install_appimage),
            1: ("Install Flatpak", self._install_flatpak),
            2: ("Install package", self._install_cli_package),
            3: ("Installed app actions are above", None),
            4: ("Choose an install method", None),
        }
        text, callback = actions[selected]
        self.primary_action_callback = callback
        self.primary_action.configure(text=text, command=callback or (lambda: None), state=("normal" if callback else "disabled"))

    def _run_primary_action(self) -> None:
        callback = self.primary_action_callback
        if callback:
            callback()

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _refresh_tool_status(self) -> None:
        self.flatpak_status.configure(text="Flatpak is available." if shutil.which("flatpak") else "Flatpak is not installed or not on PATH.")
        found = [tool for tool in ("pipx", "cargo", "npm") if shutil.which(tool)]
        self.cli_status.configure(text="Available: " + (", ".join(found) if found else "none found on PATH"))

    def _begin(self, label: str, worker) -> bool:
        if self.busy:
            messagebox.showinfo(APP_TITLE, "Another operation is still running.")
            return False
        self.busy = True
        self.status.configure(text=label)
        self.progress.pack(side="right", padx=(0, 10))
        self.progress.start(12)
        threading.Thread(target=worker, daemon=True).start()
        return True

    def _finish(self, message: str) -> None:
        self.events.put(("finish", message))

    def _drain_events(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(str(value))
            elif kind == "status":
                self.status.configure(text=str(value))
            elif kind == "finish":
                self.busy = False
                self.progress.stop()
                self.progress.pack_forget()
                self.status.configure(text=str(value))
            elif kind == "apps":
                self._populate_apps(value)
            elif kind == "info":
                messagebox.showinfo(APP_TITLE, str(value))
            elif kind == "error":
                messagebox.showerror(APP_TITLE, str(value))
        self.after(100, self._drain_events)

    def _confirmed(self, title: str, summary: str, commands: list[list[str]] | None = None) -> bool:
        detail = summary
        if commands:
            detail += "\n\nPlanned steps:\n" + "\n".join("  " + " ".join(command) for command in commands)
        detail += "\n\nThis runs with your account’s normal permissions. Continue?"
        return messagebox.askyesno(title, detail, icon="warning")

    def _install_appimage(self) -> None:
        name = self.app_name.get().strip()
        url = self.app_url.get().strip()
        checksum = self.app_hash.get().strip().lower()
        if not name or not re.fullmatch(r"[\w .+-]{1,80}", name, flags=re.UNICODE):
            messagebox.showerror(APP_TITLE, "Enter a simple application name (letters, numbers, spaces, . + or -).")
            return
        if not valid_https_url(url):
            messagebox.showerror(APP_TITLE, "Enter a valid HTTPS download URL.")
            return
        if checksum and not re.fullmatch(r"[0-9a-f]{64}", checksum):
            messagebox.showerror(APP_TITLE, "SHA-256 must be exactly 64 hexadecimal characters.")
            return
        if not urllib.parse.urlparse(url).path.lower().endswith(".appimage"):
            messagebox.showerror(APP_TITLE, "The direct download URL must end in .AppImage.")
            return
        if not self._confirmed("Confirm AppImage install", f"Download {name} from:\n{url}\n\nThe file will be executable as your user."):
            return
        self._begin("Downloading AppImage…", lambda: self._download_appimage(name, url, checksum))

    def _download_appimage(self, name: str, url: str, checksum: str) -> None:
        try:
            APPIMAGE_HOME.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "application"
            destination = APPIMAGE_HOME / f"{slug}.AppImage"
            if destination.exists():
                raise FileExistsError(f"{destination} already exists. Remove or rename it before reinstalling.")
            temporary = destination.with_suffix(".AppImage.part")
            digest = hashlib.sha256()
            count = 0
            request = urllib.request.Request(url, headers={"User-Agent": f"{APP_ID}/{APP_VERSION}"})
            try:
                with urllib.request.urlopen(request, timeout=30) as response, temporary.open("xb") as out:
                    if not valid_https_url(response.geturl()):
                        raise ValueError("The download redirected away from HTTPS; download stopped.")
                    total = int(response.headers.get("Content-Length", "0") or 0)
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        count += len(block)
                        if count > MAX_APPIMAGE_BYTES:
                            raise ValueError("Download exceeded the 2 GiB size limit.")
                        digest.update(block)
                        out.write(block)
                        if total:
                            self.events.put(("status", f"Downloading AppImage… {min(100, count * 100 // total)}%"))
                actual = digest.hexdigest()
                if checksum and actual != checksum:
                    raise ValueError(f"SHA-256 mismatch. Expected {checksum}; received {actual}.")
                os.chmod(temporary, 0o755)
                temporary.replace(destination)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            desktop = APPLICATIONS_HOME / f"{slug}.desktop"
            desktop.parent.mkdir(parents=True, exist_ok=True)
            desktop.write_text(
                "[Desktop Entry]\nType=Application\n"
                f"Name={name}\nExec={desktop_quote(str(destination))}\n"
                "Terminal=false\nCategories=Utility;\nStartupNotify=true\n",
                encoding="utf-8",
            )
            self.events.put(("log", f"Installed {name}\nLocation: {destination}\nSHA-256: {actual}\nMenu entry: {desktop}"))
            self._finish("AppImage installed")
        except Exception as exc:
            self.events.put(("log", f"AppImage install failed: {exc}"))
            self._finish("Install failed")

    def _install_flatpak(self) -> None:
        app_id = self.flatpak_id.get().strip()
        remote = self.flatpak_remote.get().strip()
        if not shutil.which("flatpak"):
            messagebox.showerror(APP_TITLE, "Flatpak is not installed or not on PATH.")
            return
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,180}", app_id):
            messagebox.showerror(APP_TITLE, "Enter a valid Flatpak application ID.")
            return
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", remote):
            messagebox.showerror(APP_TITLE, "Enter a valid remote name.")
            return
        commands: list[list[str]] = []
        summary = f"Install {app_id} from {remote} for your account."
        if remote == "flathub":
            commands.append(["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", FLATHUB_REPO_URL])
            summary += " If needed, the official Flathub remote will be added for your account first."
        commands.append(["flatpak", "install", "--user", "--assumeyes", remote, app_id])
        if not self._confirmed("Confirm Flatpak install", summary, commands):
            return
        self._begin("Installing Flatpak…", lambda: self._run_commands(commands, refresh_apps=True))

    def _install_cli_package(self) -> None:
        tool = self.cli_tool.get()
        package = self.cli_package.get().strip()
        patterns = {
            "pipx": r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?(?:==[A-Za-z0-9.*+!-]+)?",
            "cargo": r"[A-Za-z0-9][A-Za-z0-9_-]*",
            "npm": r"(?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9][A-Za-z0-9._/-]*(?:@[A-Za-z0-9._+-]+)?",
        }
        if tool not in patterns or not re.fullmatch(patterns[tool], package):
            messagebox.showerror(APP_TITLE, "Enter a package name, not a command or URL.")
            return
        if not shutil.which(tool):
            messagebox.showerror(APP_TITLE, f"{tool} is not installed or not on PATH. This app won’t install it for you.")
            return
        prefix = APP_HOME / tool
        command = (["pipx", "install", package] if tool == "pipx" else
                   ["cargo", "install", "--root", str(prefix), package] if tool == "cargo" else
                   ["npm", "install", "--global", "--prefix", str(prefix), package])
        summary = (
            f"Install {package} using {tool}. Package hooks can run code as your account "
            "and may request elevated permissions. This app does not invoke sudo and is not a sandbox."
        )
        if not self._confirmed(f"Confirm {tool} install", summary, [command]):
            return
        self._begin(f"Installing with {tool}…", lambda: self._run_commands([command]))

    def _run_commands(self, commands: list[list[str]], refresh_apps: bool = False) -> None:
        try:
            env = os.environ.copy()
            env.update({"CI": "1", "PIP_NO_INPUT": "1", "GIT_TERMINAL_PROMPT": "0"})
            for index, command in enumerate(commands):
                self.events.put(("log", "Running: " + " ".join(command)))
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
                assert process.stdout is not None
                # Read carriage-return progress too; line iteration hides it until completion.
                buffer: list[str] = []
                while True:
                    char = process.stdout.read(1)
                    if char == "":
                        break
                    if char in ("\r", "\n"):
                        line = "".join(buffer).strip()
                        if line:
                            self.events.put(("log", line))
                        buffer.clear()
                    else:
                        buffer.append(char)
                tail = "".join(buffer).strip()
                if tail:
                    self.events.put(("log", tail))
                code = process.wait()
                self.events.put(("log", f"Process exited with status {code}."))
                if code != 0:
                    self._finish("Install failed")
                    return
                if index + 1 < len(commands):
                    self.events.put(("log", "Flathub is ready. Starting app installation…"))
            if refresh_apps:
                self._fetch_installed()
            self._finish("Install complete")
        except Exception as exc:
            self.events.put(("log", f"Operation failed: {exc}"))
            self._finish("Install failed")

    def _refresh_installed(self) -> None:
        if not shutil.which("flatpak"):
            self.events.put(("apps", []))
            self.events.put(("log", "Flatpak is not installed or not on PATH."))
            return
        if self.busy:
            return
        self._begin("Loading installed Flatpaks…", self._fetch_installed)

    def _fetch_installed(self) -> None:
        try:
            proc = subprocess.run(
                ["flatpak", "list", "--user", "--app", "--columns=application,name,version,origin"],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or f"flatpak exited with status {proc.returncode}")
            rows: list[tuple[str, str, str, str]] = []
            for line in proc.stdout.splitlines():
                fields = line.split("\t") if "\t" in line else re.split(r"\s{2,}", line.strip())
                if len(fields) >= 4 and fields[0].strip() and fields[0].strip() != "Application":
                    rows.append(tuple(field.strip() for field in fields[:4]))
            self.events.put(("apps", rows))
            self.events.put(("log", f"Found {len(rows)} user-installed Flatpak app(s)."))
            self._finish("Installed apps refreshed")
        except Exception as exc:
            self.events.put(("log", f"Could not list user Flatpaks: {exc}"))
            self.events.put(("apps", []))
            self._finish("Refresh failed")

    def _populate_apps(self, rows: object) -> None:
        for item in self.apps_tree.get_children():
            self.apps_tree.delete(item)
        if not isinstance(rows, list):
            return
        for row in rows:
            if isinstance(row, (tuple, list)) and len(row) == 4:
                app_id, name, version, remote = (str(value) for value in row)
                self.apps_tree.insert("", "end", values=(name, app_id, version, remote))

    def _selected_app_id(self) -> str | None:
        selected = self.apps_tree.selection()
        if not selected:
            messagebox.showinfo(APP_TITLE, "Select an app first.")
            return None
        values = self.apps_tree.item(selected[0], "values")
        return str(values[1]) if len(values) > 1 else None

    def _launch_selected_app(self) -> None:
        app_id = self._selected_app_id()
        if not app_id:
            return
        try:
            subprocess.Popen(["flatpak", "run", "--user", app_id], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self._log(f"Launched {app_id}.")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not launch {app_id}: {exc}")

    def _uninstall_selected(self) -> None:
        app_id = self._selected_app_id()
        if not app_id:
            return
        command = ["flatpak", "uninstall", "--user", "--assumeyes", app_id]
        if not self._confirmed(
            "Uninstall this Flatpak?",
            f"Uninstall {app_id} from your account. This leaves its app data and settings in your home folder.",
            [command],
        ):
            return
        self._begin(f"Uninstalling {app_id}…", lambda: self._run_commands([command], refresh_apps=True))

    def _update_flatpaks(self) -> None:
        if not shutil.which("flatpak"):
            messagebox.showerror(APP_TITLE, "Flatpak is not installed or not on PATH.")
            return
        command = ["flatpak", "update", "--user", "--assumeyes"]
        if not self._confirmed("Update user Flatpaks", "Update Flatpak apps and runtimes installed for your account.", [command]):
            return
        self._begin("Updating Flatpaks…", lambda: self._run_commands([command], refresh_apps=True))

    def _choose_update(self) -> None:
        archive = filedialog.askopenfilename(
            title="Choose a Local Install Center update archive",
            filetypes=(("Local Install Center archives", "*.tar.gz *.tgz"), ("All files", "*")),
        )
        if not archive:
            return
        path = Path(archive)
        try:
            manifest = self._read_update_manifest(path)
            version = str(manifest["version"])
            if version_tuple(version) <= version_tuple(APP_VERSION):
                raise ValueError(f"Selected version {version} is not newer than {APP_VERSION}.")
            if manifest.get("application") != APP_ID:
                raise ValueError("This archive is not a Local Install Center release.")
        except Exception as exc:
            messagebox.showerror("Update not accepted", str(exc))
            return
        if not self._confirmed(
            "Install Local Install Center update?",
            f"Install version {version} from:\n{path}\n\nThe new release will be placed in its own folder. Older installed versions will be kept. The menu launcher switches only after files are checked and copied.",
        ):
            return
        self._begin(f"Installing version {version}…", lambda: self._install_update_archive(path, version))

    def _read_update_manifest(self, archive: Path) -> dict[str, object]:
        if not archive.is_file() or archive.stat().st_size > MAX_UPDATE_ARCHIVE_BYTES:
            raise ValueError("The update archive is missing or larger than the 64 MiB limit.")
        with tarfile.open(archive, mode="r:gz") as tf:
            try:
                member = tf.getmember("local-install-center/release.json")
            except KeyError as exc:
                raise ValueError("The archive does not contain release metadata.") from exc
            if not member.isfile() or member.size > 16_384:
                raise ValueError("The release metadata is invalid or too large.")
            stream = tf.extractfile(member)
            if stream is None:
                raise ValueError("Could not read release metadata.")
            manifest = json.loads(stream.read().decode("utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("Release metadata must be a JSON object.")
        version_tuple(str(manifest.get("version", "")))
        return manifest

    def _install_update_archive(self, archive: Path, expected_version: str) -> None:
        temp_parent: Path | None = None
        staged_release: Path | None = None
        try:
            RELEASES_HOME.mkdir(parents=True, exist_ok=True)
            target = RELEASES_HOME / expected_version
            if target.exists():
                raise FileExistsError(f"Version {expected_version} is already installed. Existing release folders are kept.")
            temp_parent = Path(tempfile.mkdtemp(prefix=".update-", dir=RELEASES_HOME))
            package_root = temp_parent / "package"
            package_root.mkdir()
            with tarfile.open(archive, mode="r:gz") as tf:
                members: list[tarfile.TarInfo] = []
                for member in tf:
                    if len(members) >= MAX_UPDATE_FILES:
                        raise ValueError("The update contains too many files.")
                    members.append(member)
                total_size = 0
                safe_members: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
                for member in members:
                    rel = PurePosixPath(member.name)
                    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "local-install-center":
                        raise ValueError("The update archive contains an unsafe path.")
                    if not (member.isfile() or member.isdir()):
                        raise ValueError("The update archive contains a link or unsupported file type.")
                    total_size += max(0, member.size)
                    if total_size > MAX_UPDATE_EXPANDED_BYTES:
                        raise ValueError("The unpacked update exceeds the 128 MiB limit.")
                    safe_members.append((member, rel))
                for member, rel in safe_members:
                    parts = rel.parts[1:]
                    if not parts:
                        continue
                    destination = package_root.joinpath(*parts)
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = tf.extractfile(member)
                    if source is None:
                        raise ValueError("A file in the update could not be read.")
                    with source, destination.open("xb") as out:
                        shutil.copyfileobj(source, out)
                    if member.mode & 0o111:
                        os.chmod(destination, 0o755)
                manifest_path = package_root / "release.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("application") != APP_ID or str(manifest.get("version")) != expected_version:
                    raise ValueError("Release metadata doesn’t match the selected update version.")
                app_path = package_root / "install_center.py"
                icon_path = package_root / ICON_RELATIVE
                if not app_path.is_file() or not icon_path.is_file():
                    raise ValueError("The update is missing required app files or its icon.")
                ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))
            staged_release = RELEASES_HOME / f".{expected_version}-{uuid.uuid4().hex}.tmp"
            shutil.copytree(package_root, staged_release)
            os.replace(staged_release, target)
            desktop = APPLICATIONS_HOME / f"{APP_ID}.desktop"
            self._write_launcher(target, desktop, expected_version)
            self.events.put(("log", f"Installed version {expected_version} at {target}. Older release folders were kept."))
            self.events.put(("info", f"Version {expected_version} is installed. Close this window and reopen Local Install Center from the menu to use it."))
            self._finish("Update installed · restart from menu")
        except Exception as exc:
            self.events.put(("log", f"Update failed: {exc}"))
            self._finish("Update failed · current launcher unchanged")
        finally:
            if staged_release and staged_release.exists():
                shutil.rmtree(staged_release, ignore_errors=True)
            if temp_parent and temp_parent.exists():
                shutil.rmtree(temp_parent, ignore_errors=True)

    def _write_launcher(self, release_dir: Path, destination: Path, version: str) -> None:
        app_script = release_dir / "install_center.py"
        icon_path = release_dir / ICON_RELATIVE
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "[Desktop Entry]\nType=Application\n"
            f"Name={APP_TITLE}\n"
            f"Exec={desktop_quote(sys.executable)} {desktop_quote(str(app_script))}\n"
            f"Icon={icon_path}\n"
            "Terminal=false\nCategories=Utility;System;\nStartupNotify=true\n"
            f"Comment=Install and manage user-level Linux apps (v{version})\n"
        )
        temporary = destination.with_suffix(".desktop.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)


if __name__ == "__main__":
    try:
        InstallCenter().mainloop()
    except tk.TclError as exc:
        print(f"Could not start the graphical interface: {exc}", file=sys.stderr)
        raise SystemExit(1)
