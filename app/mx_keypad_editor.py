#!/usr/bin/env python3
"""MX Keypad Editor: a GTK4/libadwaita editor for the mx-keypad daemon's config.json.

Previews come from `node render-server.js <configDir>` and the Test button runs
`node run-action.js`, both from the repo this file lives in (the parent of app/).
"""
import argparse
import base64
import copy
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

APP_ID = "io.github.orbxom.MxKeypadEditor"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
KEY_COUNT = 9
KEY_SIZE = 118
DEFAULT_BG = "#000000"
DEFAULT_FG = "#ffffff"
KITTY_PASTE = [29, 42, 47]
CTRL_V_PASTE = [29, 47]
PASTE_CHORDS = [("Ctrl+Shift+V (kitty, terminals)", KITTY_PASTE), ("Ctrl+V (most apps)", CTRL_V_PASTE)]
MUTED_ON = {"sub": "MUTED", "bg": "#a01010"}

OPENWHISPR = "dbus-send --session --type=method_call --dest=com.openwhispr.App /com/openwhispr/App com.openwhispr.App."

ACTION_TYPES = [
    ("none", "None"),
    ("command", "Shell command"),
    ("app", "Launch app"),
    ("media", "Media / volume"),
    ("keys", "Keyboard shortcut"),
    ("prompt", "Prompt (paste text)"),
    ("builtin", "Keypad brightness"),
]
TYPE_IDS = [t for t, _ in ACTION_TYPES]

# (title, label, action, state) - state is a shell command; exit 0 means "on".
MEDIA_PRESETS = [
    ("Play / pause", "Play", {"type": "keys", "codes": [164]}, None),
    ("Next track", "Next", {"type": "keys", "codes": [163]}, None),
    ("Previous track", "Prev", {"type": "keys", "codes": [165]}, None),
    ("Volume up", "Vol +", {"type": "keys", "codes": [115]}, None),
    ("Volume down", "Vol -", {"type": "keys", "codes": [114]}, None),
    ("Mute speakers", "Mute", {"type": "keys", "codes": [113]},
     "wpctl get-volume @DEFAULT_AUDIO_SINK@ | grep -q MUTED"),
    ("Mute microphone", "Mic", {"type": "command", "command": "wpctl set-mute @DEFAULT_AUDIO_SOURCE@ toggle"},
     "wpctl get-volume @DEFAULT_AUDIO_SOURCE@ | grep -q MUTED"),
]
MEDIA_STATES = {p[3] for p in MEDIA_PRESETS if p[3]}

# (title, subtitle, key fields) - applied in one click, replacing the key's look and action.
AI_PRESETS = [
    ("OpenWhispr dictation", "Start / stop dictation",
     {"label": "Dictate", "sub": "whispr", "bg": "#5b2a86", "fg": "#ffffff",
      "action": {"type": "command", "command": OPENWHISPR + "Toggle"}}),
    ("OpenWhispr voice agent", "Start / stop the voice agent",
     {"label": "Agent", "sub": "voice", "bg": "#7a3b9e", "fg": "#ffffff",
      "action": {"type": "command", "command": OPENWHISPR + "ToggleVoiceAgent"}}),
    ("Claude Code: Esc", "Interrupt Claude",
     {"label": "Esc", "sub": "claude", "bg": "#8a3a1c", "fg": "#ffffff", "action": {"type": "keys", "codes": [1]}}),
    ("Claude Code: Shift+Tab", "Cycle permission mode",
     {"label": "Mode", "sub": "shift+tab", "bg": "#6b4a1c", "fg": "#ffffff",
      "action": {"type": "keys", "codes": [42, 15]}}),
    ("New prompt key", "Paste a canned prompt into kitty",
     {"label": "Prompt", "sub": "claude", "bg": "#c15f3c", "fg": "#ffffff",
      "action": {"type": "prompt", "text": "", "submit": False, "restoreClipboard": False}}),
]

EVDEV_NAMES = {
    1: "Esc", 12: "-", 13: "=", 14: "Backspace", 15: "Tab", 26: "[", 27: "]", 28: "Enter", 29: "Ctrl",
    39: ";", 40: "'", 41: "`", 42: "Shift", 43: "\\", 51: ",", 52: ".", 53: "/", 54: "RShift", 55: "KP*",
    56: "Alt", 57: "Space", 58: "CapsLock", 87: "F11", 88: "F12", 96: "KPEnter", 97: "RCtrl", 99: "Print",
    100: "AltGr", 102: "Home", 103: "Up", 104: "PgUp", 105: "Left", 106: "Right", 107: "End", 108: "Down",
    109: "PgDn", 110: "Insert", 111: "Delete", 113: "Mute", 114: "VolDown", 115: "VolUp", 119: "Pause",
    125: "Super", 126: "RSuper", 127: "Menu", 163: "Next", 164: "PlayPause", 165: "Previous",
}
for _i, _c in enumerate("1234567890"):
    EVDEV_NAMES[2 + _i] = _c
for _row, _start in (("QWERTYUIOP", 16), ("ASDFGHJKL", 30), ("ZXCVBNM", 44)):
    for _i, _c in enumerate(_row):
        EVDEV_NAMES[_start + _i] = _c
for _i in range(10):
    EVDEV_NAMES[59 + _i] = f"F{_i + 1}"

CSS = f"""
.mxkey {{ padding: 0; min-width: {KEY_SIZE}px; min-height: {KEY_SIZE}px; border-radius: 10px; }}
.mxkey.selected {{ outline: 3px solid @accent_color; outline-offset: 2px; }}
.mxkey-empty {{ font-size: 28px; opacity: 0.35; }}
.mxkey-label {{ font-weight: bold; font-size: 20px; }}
.mxkey-sub {{ font-size: 12px; opacity: 0.8; }}
.prompt-text {{ font-family: monospace; padding: 6px; }}
.chord {{ font-size: 22px; font-weight: bold; }}
"""


# ---------------------------------------------------------------- config model

def find_node():
    env = os.environ.get("MX_KEYPAD_NODE")
    if env and os.access(env, os.X_OK):
        return env
    path = shutil.which("node")
    if path:
        return path

    def version(p):
        m = re.search(r"/v(\d+)\.(\d+)\.(\d+)/", p)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

    found = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")), key=version)
    return found[-1] if found else None


def serialize(config):
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


def parse_config(text):
    config = json.loads(text)
    if not isinstance(config, dict) or not isinstance(config.get("pages"), list) or not config["pages"]:
        raise ValueError('config.json needs a non-empty "pages" array')
    for page in config["pages"]:
        if not isinstance(page, dict):
            raise ValueError("every page must be an object")
    return config


def legacy_action(key):
    if key.get("builtin"):
        return {"type": "builtin", "name": key["builtin"]}
    if key.get("run"):
        return {"type": "command", "command": key["run"]}
    return None


def key_action(key):
    """The action a key runs, honouring the precedence action > builtin > run."""
    if not key:
        return None
    return key.get("action") or legacy_action(key)


def convert_legacy(key):
    """On edit, a legacy key gets an `action` and loses `run`/`builtin` (it keeps `state`/`on`)."""
    if "run" in key or "builtin" in key:
        if "action" not in key:
            action = legacy_action(key)
            if action:
                key["action"] = action
        key.pop("run", None)
        key.pop("builtin", None)


def media_preset_index(action, state=None):
    if not action:
        return -1
    for i, (_, _, preset, _) in enumerate(MEDIA_PRESETS):
        if action.get("type") == preset["type"] == "keys" and action.get("codes") == preset["codes"]:
            return i
        if action.get("type") == preset["type"] == "command" and action.get("command") == preset["command"]:
            return i
    return -1


def form_type(action):
    if not action:
        return "none"
    if media_preset_index(action) >= 0:
        return "media"
    return action.get("type") if action.get("type") in TYPE_IDS else "none"


def chord_text(codes):
    return "+".join(EVDEV_NAMES.get(c, str(c)) for c in codes) if codes else "(none)"


def hex_to_rgba(value, fallback):
    rgba = Gdk.RGBA()
    if not (isinstance(value, str) and rgba.parse(value)):
        rgba.parse(fallback)
    return rgba


def rgba_to_hex(rgba):
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(c * 255))) for c in (rgba.red, rgba.green, rgba.blue)))


def sniff_image_ext(data, name):
    lower = name.lower()
    if data.startswith(b"\x89PNG"):
        return "png"
    if lower.endswith(".svg") or b"<svg" in data[:2048]:
        return "svg"
    return None


def store_icon(config_dir, data, name):
    """Copy icon bytes to icons/<sha1>.<ext> in the config dir; returns the relative path."""
    ext = sniff_image_ext(data, name)
    if not ext:
        raise ValueError(f"{os.path.basename(name)} is not a PNG or SVG")
    rel = f"icons/{hashlib.sha1(data).hexdigest()}.{ext}"
    dest = os.path.join(config_dir, rel)
    if not os.path.exists(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
    return rel


# ---------------------------------------------------------------- render server client

class RenderClient:
    """Talks to a persistent `node render-server.js <configDir>` over line-delimited JSON.

    Requests are debounced per slot and replies matched by id; the server is respawned when it exits.
    When it can't run, `on_result(slot, None)` keeps the plain coloured fallback on screen.
    """

    DEBOUNCE_MS = 100
    MAX_CRASHES = 3

    def __init__(self, config_dir, on_result, enabled=True):
        self.config_dir = config_dir
        self.on_result = on_result
        self.enabled = enabled
        self.script = os.path.join(REPO_DIR, "render-server.js")
        self.node = find_node()
        self.proc = self.stdin = self.stdout = None
        self.cancel = None
        self.next_id = 1
        self.pending = {}   # id -> slot
        self.latest = {}    # slot -> newest id sent
        self.wanted = {}    # slot -> key waiting for its debounce timer
        self.timers = {}
        self.last_keys = {}  # slot -> last key requested, re-sent after a respawn
        self.crashes = 0
        self.got_reply = False
        self.respawn_source = 0

    def _start(self):
        if self.proc or not self.enabled or self.crashes >= self.MAX_CRASHES:
            return self.proc is not None
        if not self.node or not os.path.exists(self.script):
            return False
        try:
            launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE)
            launcher.set_cwd(REPO_DIR)
            self.proc = launcher.spawnv([self.node, self.script, self.config_dir])
        except GLib.Error as e:
            print(f"render-server failed to start: {e.message}", file=sys.stderr)
            self.crashes += 1
            return False
        self.stdin = self.proc.get_stdin_pipe()
        self.stdout = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self.cancel = Gio.Cancellable()
        self.got_reply = False
        self._read_next(self.proc)
        return True

    def _read_next(self, proc):
        self.stdout.read_line_async(GLib.PRIORITY_DEFAULT, self.cancel, self._on_line, proc)

    def _on_line(self, stream, result, proc):
        try:
            line, _ = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if proc is not self.proc:
            return
        if line is None:
            self._on_exit()
            return
        self._handle_reply(line)
        self._read_next(proc)

    def _handle_reply(self, line):
        try:
            msg = json.loads(line)
        except ValueError:
            return
        slot = self.pending.pop(msg.get("id"), None)
        if slot is None or self.latest.get(slot) != msg.get("id"):
            return
        self.got_reply = True
        self.crashes = 0
        png = None
        if isinstance(msg.get("png"), str):
            try:
                png = base64.b64decode(msg["png"])
            except ValueError:
                png = None
        elif msg.get("error"):
            print(f"render-server: slot {slot}: {msg['error']}", file=sys.stderr)
        self.on_result(slot, png)

    def _on_exit(self):
        if not self.got_reply:
            self.crashes += 1
        self.stop()
        if self.crashes >= self.MAX_CRASHES:
            print("render-server keeps exiting; using plain previews", file=sys.stderr)
            return
        # Re-request whatever was in flight once the server is back.
        lost = {slot for slot in self.pending.values()}
        self.pending.clear()
        self.respawn_source = GLib.timeout_add(1000 * max(1, self.crashes), self._respawn, lost)

    def _respawn(self, slots):
        self.respawn_source = 0
        for slot in slots:
            if slot in self.last_keys:
                self.request(slot, self.last_keys[slot])
        return GLib.SOURCE_REMOVE

    def request(self, slot, key):
        self.wanted[slot] = self.last_keys[slot] = copy.deepcopy(key)
        if slot not in self.timers:
            self.timers[slot] = GLib.timeout_add(self.DEBOUNCE_MS, self._flush, slot)

    def cancel_slot(self, slot):
        """Drop a slot's pending render, so a late reply can't repaint a slot that is now empty."""
        source = self.timers.pop(slot, None)
        if source:
            GLib.source_remove(source)
        self.wanted.pop(slot, None)
        self.latest.pop(slot, None)
        self.last_keys.pop(slot, None)

    def forget(self):
        """Drop outstanding work, e.g. when the visible page changes."""
        for source in self.timers.values():
            GLib.source_remove(source)
        self.timers.clear()
        self.wanted.clear()
        self.latest.clear()
        self.last_keys.clear()

    def _flush(self, slot):
        self.timers.pop(slot, None)
        key = self.wanted.pop(slot, None)
        if key is None:
            return GLib.SOURCE_REMOVE
        if not self._start():
            self.on_result(slot, None)
            return GLib.SOURCE_REMOVE
        req_id = self.next_id
        self.next_id += 1
        self.pending[req_id] = slot
        self.latest[slot] = req_id
        line = json.dumps({"id": req_id, "key": key, "pressed": False}, ensure_ascii=False) + "\n"
        try:
            self.stdin.write_all(line.encode(), None)
            self.stdin.flush(None)
        except GLib.Error:
            self.pending.pop(req_id, None)
            self.on_result(slot, None)
        return GLib.SOURCE_REMOVE

    def stop(self):
        proc, self.proc = self.proc, None
        if self.cancel:
            self.cancel.cancel()
        if proc:
            try:  # the server exits when its stdin closes
                self.stdin.close(None)
            except GLib.Error:
                pass
        self.stdin = self.stdout = self.cancel = None

    def shutdown(self):
        self.enabled = False
        if self.respawn_source:
            GLib.source_remove(self.respawn_source)
        self.forget()
        self.stop()


# ---------------------------------------------------------------- key button

class KeyButton(Gtk.Button):
    def __init__(self, index):
        super().__init__()
        self.index = index
        self.add_css_class("mxkey")
        self.add_css_class(f"mxkey-{index}")
        self.set_size_request(KEY_SIZE, KEY_SIZE)
        self.stack = Gtk.Stack()
        empty = Gtk.Label(label="+")
        empty.add_css_class("mxkey-empty")
        self.stack.add_named(empty, "empty")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.icon = Gtk.Image(pixel_size=40)
        self.label = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=8)
        self.label.add_css_class("mxkey-label")
        self.sub = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=12)
        self.sub.add_css_class("mxkey-sub")
        for w in (self.icon, self.label, self.sub):
            box.append(w)
        self.stack.add_named(box, "fallback")

        self.picture = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.FILL)
        self.stack.add_named(self.picture, "picture")
        self.set_child(self.stack)
        self.set_overflow(Gtk.Overflow.HIDDEN)

    def show_key(self, key, config_dir):
        if not key:
            self.stack.set_visible_child_name("empty")
            self.set_tooltip_text("Empty key")
            return
        self.label.set_text(str(key.get("label") or ""))
        self.sub.set_text(str(key.get("sub") or ""))
        self.sub.set_visible(bool(key.get("sub")))
        icon = key.get("icon")
        path = os.path.join(config_dir, icon) if icon else None
        self.icon.set_visible(bool(path and os.path.exists(path)))
        if self.icon.get_visible():
            self.icon.set_from_file(path)
        self.set_tooltip_text(describe_action(key_action(key)))
        if self.stack.get_visible_child_name() == "empty":
            self.stack.set_visible_child_name("fallback")

    def show_png(self, png):
        if png is None:
            if self.stack.get_visible_child_name() == "picture":
                self.stack.set_visible_child_name("fallback")
            return
        try:
            self.picture.set_paintable(Gdk.Texture.new_from_bytes(GLib.Bytes.new(png)))
            self.stack.set_visible_child_name("picture")
        except GLib.Error:
            self.stack.set_visible_child_name("fallback")


def describe_action(action):
    if not action:
        return "No action"
    t = action.get("type")
    if t == "command":
        return f"Command: {action.get('command', '')}"
    if t == "app":
        return f"App: {action.get('desktopId', '')}"
    if t == "keys":
        return f"Keys: {chord_text(action.get('codes') or [])}"
    if t == "prompt":
        first = (action.get("text") or "").strip().splitlines()
        return f"Prompt: {first[0][:60] if first else '(empty)'}"
    if t == "builtin":
        return f"Builtin: {action.get('name')}"
    return f"Action: {t}"


# ---------------------------------------------------------------- dialogs

class ShortcutRecorder(Adw.Window):
    """Records a chord as evdev codes (GDK keycode - 8) in press order, until the first key release."""

    def __init__(self, parent, on_done):
        super().__init__(transient_for=parent, modal=True, title="Record shortcut",
                         default_width=420, default_height=220)
        self.on_done = on_done
        self.codes = []
        self.held = set()   # codes currently down, so auto-repeat presses are ignored
        self.finished = False

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=18,
                      margin_start=18, margin_end=18)
        box.append(Gtk.Label(label="Press the shortcut. Recording stops when you release a key.", wrap=True))
        self.chord = Gtk.Label(label="…")
        self.chord.add_css_class("chord")
        box.append(self.chord)
        buttons = Gtk.Box(spacing=6, halign=Gtk.Align.END, vexpand=True, valign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        retry = Gtk.Button(label="Clear")
        retry.connect("clicked", lambda *_: self._reset())
        self.use = Gtk.Button(label="Use shortcut", sensitive=False)
        self.use.add_css_class("suggested-action")
        self.use.connect("clicked", self._accept)
        for b in (cancel, retry, self.use):
            b.set_focus_on_click(False)
            buttons.append(b)
        box.append(buttons)
        view.set_content(box)
        self.set_content(view)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_press)
        keys.connect("key-released", self._on_release)
        self.add_controller(keys)
        self.connect("map", lambda *_: GLib.idle_add(self._inhibit))
        self.connect("close-request", self._on_close)

    def _inhibit(self):
        surface = self.get_surface()
        if isinstance(surface, Gdk.Toplevel):
            surface.inhibit_system_shortcuts(None)
        return GLib.SOURCE_REMOVE

    def _on_close(self, *_):
        surface = self.get_surface()
        if isinstance(surface, Gdk.Toplevel):
            surface.restore_system_shortcuts()
        return False

    def _reset(self):
        self.codes = []
        self.held.clear()
        self.finished = False
        self.chord.set_text("…")
        self.use.set_sensitive(False)

    def _on_press(self, _ctrl, _keyval, keycode, _state):
        code = keycode - 8
        if code <= 0 or code in self.held:  # auto-repeat of a key already down
            return True
        if self.finished:
            if self.held:  # the finished chord is still being let go of
                self.held.add(code)
                return True
            self._reset()  # a fresh key with nothing held starts a new chord
        self.held.add(code)
        if code not in self.codes:
            self.codes.append(code)
            self.chord.set_text(chord_text(self.codes))
        return True

    def _on_release(self, _ctrl, _keyval, keycode, _state):
        self.held.discard(keycode - 8)
        if self.codes:
            self.finished = True
            self.use.set_sensitive(True)
        return True

    def _accept(self, *_):
        codes = list(self.codes)
        self.close()
        if codes:
            self.on_done(codes)


class AppChooser(Adw.Window):
    def __init__(self, parent, on_done):
        super().__init__(transient_for=parent, modal=True, title="Choose an app",
                         default_width=440, default_height=560)
        self.on_done = on_done
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        self.search = Gtk.SearchEntry(placeholder_text="Search apps")
        self.search.connect("search-changed", lambda *_: self.listbox.invalidate_filter())
        self.search.connect("activate", self._activate_first)
        box.append(self.search)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_filter_func(self._filter)
        self.listbox.connect("row-activated", lambda _lb, row: self._choose(row.app))
        apps = [a for a in Gio.AppInfo.get_all() if a.should_show() and a.get_id()]
        for app in sorted(apps, key=lambda a: (a.get_display_name() or "").lower()):
            row = Adw.ActionRow(title=GLib.markup_escape_text(app.get_display_name() or app.get_id()),
                                subtitle=GLib.markup_escape_text(app.get_id()), activatable=True)
            row.app = app
            row.search_text = f"{app.get_display_name()} {app.get_id()}".lower()
            image = Gtk.Image(pixel_size=32)
            if app.get_icon():
                image.set_from_gicon(app.get_icon())
            row.add_prefix(image)
            self.listbox.append(row)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.listbox)
        box.append(scroller)
        view.set_content(box)
        self.set_content(view)

    def _filter(self, row):
        text = self.search.get_text().strip().lower()
        return not text or all(word in row.search_text for word in text.split())

    def _activate_first(self, *_):
        row = self.listbox.get_first_child()
        while row is not None:
            if isinstance(row, Gtk.ListBoxRow) and row.get_child_visible() and self._filter(row):
                self._choose(row.app)
                return
            row = row.get_next_sibling()

    def _choose(self, app):
        self.close()
        self.on_done(app)


def app_icon_file(app_info, widget):
    """The on-disk (or resource) file behind an app's icon, via the current icon theme."""
    gicon = app_info.get_icon() if app_info else None
    if gicon is None:
        return None
    theme = Gtk.IconTheme.get_for_display(widget.get_display())
    if isinstance(gicon, Gio.ThemedIcon) and not theme.has_gicon(gicon):
        return None
    # lookup_by_gicon never returns None: a missing icon comes back as the bundled "image-missing".
    paintable = theme.lookup_by_gicon(gicon, 256, 1, Gtk.TextDirection.NONE, 0)
    if paintable is None or paintable.get_icon_name() == "image-missing":
        return None
    file = paintable.get_file()
    if file is None or file.get_uri_scheme() == "resource":
        return None
    return file


def find_app(desktop_id):
    if not desktop_id:
        return None
    try:
        return Gio.DesktopAppInfo.new(desktop_id)
    except TypeError:
        return None


# ---------------------------------------------------------------- main window

CHANGED_BANNER = "config.json changed outside the editor"


class EditorWindow(Adw.ApplicationWindow):
    def __init__(self, app, config_path, render_enabled=True):
        super().__init__(application=app, title="MX Keypad Editor", default_width=1000, default_height=720)
        self.config_path = config_path
        self.config_dir = os.path.dirname(config_path)
        self.config = {"pages": [{"name": "Page 1", "keys": [None] * KEY_COUNT}]}
        self.base_text = ""     # text the in-memory config was loaded from or last saved as
        self.load_error = None  # parse error of config.json on disk; Save stays disabled while set
        self.saved_snapshot = serialize(self.config)
        self.page = 0
        self.slot = 0
        self.key_clipboard = None
        self.loading = False
        self.force_close = False
        self.monitor_timer = 0
        self.css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(self.get_display(), self.css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.render = RenderClient(self.config_dir, self._on_render, enabled=render_enabled)

        self._build_ui()
        self._install_actions()
        self.connect("close-request", self._on_close_request)
        self.load_from_disk(initial=True)
        self._watch_config()

    # ----- UI construction

    def _build_ui(self):
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title="MX Keypad Editor", subtitle=self.config_path)
        header.set_title_widget(self.title_widget)

        self.page_dropdown = Gtk.DropDown(model=Gtk.StringList(), tooltip_text="Page")
        self.page_dropdown.connect("notify::selected", self._on_page_selected)
        header.pack_start(self.page_dropdown)
        menu = Gio.Menu()
        menu.append("Add page", "win.page-add")
        menu.append("Rename page…", "win.page-rename")
        menu.append("Move page left", "win.page-left")
        menu.append("Move page right", "win.page-right")
        menu.append("Delete page…", "win.page-delete")
        header.pack_start(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="Page"))

        self.save_button = Gtk.Button(label="Save", action_name="win.save", tooltip_text="Save (Ctrl+S)")
        self.save_button.add_css_class("suggested-action")
        header.pack_end(self.save_button)
        view.add_top_bar(header)

        self.banner = Adw.Banner(title=CHANGED_BANNER, button_label="Reload")
        self.banner.connect("button-clicked", lambda *_: self._confirm_reload())
        view.add_top_bar(self.banner)

        content = Gtk.Box(spacing=0)
        content.append(self._build_grid())
        content.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        content.append(self._build_editor())
        view.set_content(content)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

    def _build_grid(self):
        grid = Gtk.Grid(row_spacing=14, column_spacing=14, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                        margin_start=24, margin_end=24, margin_top=24, margin_bottom=24, hexpand=True)
        self.buttons = []
        for i in range(KEY_COUNT):
            button = KeyButton(i)
            button.connect("clicked", lambda _b, i=i: self.select_slot(i))

            drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
            drag.connect("prepare", lambda _s, _x, _y, i=i: Gdk.ContentProvider.new_for_value(f"{self.page}:{i}"))
            drag.connect("drag-begin", lambda src, _d, b=button: src.set_icon(Gtk.WidgetPaintable.new(b), 20, 20))
            button.add_controller(drag)
            drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
            drop.connect("drop", lambda _t, value, _x, _y, i=i: self.swap_from_payload(value, self.page, i))
            button.add_controller(drop)

            click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
            click.connect("pressed", lambda _g, _n, x, y, b=button: self._context_menu(b, x, y))
            button.add_controller(click)
            grid.attach(button, i % 3, i // 3, 1, 1)
            self.buttons.append(button)

        hint = Gtk.Label(label="Drag keys to swap them. Right-click for copy, paste and clear.", wrap=True)
        hint.add_css_class("dim-label")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER, hexpand=True)
        box.append(grid)
        box.append(hint)
        return box

    def _context_menu(self, button, x, y):
        self.select_slot(button.index)
        menu = Gio.Menu()
        for label, name in (("Copy", "key-copy"), ("Paste", "key-paste"), ("Clear", "key-clear")):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(f"win.{name}", GLib.Variant("i", button.index))
            menu.append_item(item)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(button)
        popover.set_has_arrow(False)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def _build_editor(self):
        page = Adw.PreferencesPage(width_request=440)

        look = Adw.PreferencesGroup(title="Key")
        self.slot_title = look
        self.label_row = Adw.EntryRow(title="Label")
        self.label_row.connect("changed", lambda r: self._set_field("label", r.get_text()))
        self.sub_row = Adw.EntryRow(title="Sub-label")
        self.sub_row.connect("changed", lambda r: self._set_field("sub", r.get_text()))
        look.add(self.label_row)
        look.add(self.sub_row)
        self.bg_button = self._color_row(look, "Background", "bg")
        self.fg_button = self._color_row(look, "Text colour", "fg")

        self.icon_row = Adw.ActionRow(title="Icon", subtitle="None")
        self.icon_preview = Gtk.Image(pixel_size=32)
        self.icon_row.add_prefix(self.icon_preview)
        for icon_name, tip, handler in (("document-open-symbolic", "Choose an icon file (PNG or SVG)", self._pick_icon_file),
                                        ("application-x-executable-symbolic", "Use the app's icon", self._use_app_icon),
                                        ("edit-clear-symbolic", "Remove the icon", self._clear_icon)):
            b = Gtk.Button(icon_name=icon_name, tooltip_text=tip, valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("clicked", handler)
            self.icon_row.add_suffix(b)
            if handler == self._use_app_icon:
                self.app_icon_button = b
        look.add(self.icon_row)
        page.add(look)

        action = Adw.PreferencesGroup(title="Action")
        test = Gtk.Button(label="Test", valign=Gtk.Align.CENTER, tooltip_text="Run this action now")
        test.connect("clicked", lambda *_: self.test_action())
        self.test_button = test
        action.set_header_suffix(test)
        self.type_row = Adw.ComboRow(title="Type", model=Gtk.StringList.new([n for _, n in ACTION_TYPES]))
        self.type_row.connect("notify::selected", self._on_type_changed)
        action.add(self.type_row)
        page.add(action)

        self.forms = {}
        # command
        g = Adw.PreferencesGroup()
        self.command_row = Adw.EntryRow(title="Shell command")
        self.command_row.connect("changed", lambda r: self._set_action_field("command", r.get_text()))
        g.add(self.command_row)
        self.forms["command"] = g
        # app
        g = Adw.PreferencesGroup()
        self.app_row = Adw.ActionRow(title="App", subtitle="None chosen")
        self.app_image = Gtk.Image(pixel_size=32)
        self.app_row.add_prefix(self.app_image)
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda *_: AppChooser(self, self._on_app_chosen).present())
        self.app_row.add_suffix(choose)
        g.add(self.app_row)
        self.forms["app"] = g
        # media
        g = Adw.PreferencesGroup(description="Sets the action, and a state check where it has one. "
                                             "Label and colour are filled in only if the key has no label.")
        self.media_checks = []
        for i, (title, _label, preset, state) in enumerate(MEDIA_PRESETS):
            sub = describe_action(preset) + (" (shows MUTED)" if state else "")
            row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(sub), activatable=True)
            check = Gtk.Image(icon_name="object-select-symbolic", visible=False)
            row.add_suffix(check)
            row.connect("activated", lambda _r, i=i: self.apply_media_preset(i))
            self.media_checks.append(check)
            g.add(row)
        self.forms["media"] = g
        # keys
        g = Adw.PreferencesGroup()
        self.chord_row = Adw.ActionRow(title="Shortcut", subtitle="(none)")
        record = Gtk.Button(label="Record…", valign=Gtk.Align.CENTER)
        record.connect("clicked", lambda *_: ShortcutRecorder(self, self._set_codes).present())
        self.chord_row.add_suffix(record)
        g.add(self.chord_row)
        self.codes_row = Adw.EntryRow(title="Evdev codes (comma separated)")
        self.codes_row.connect("changed", self._on_codes_typed)
        g.add(self.codes_row)
        self.forms["keys"] = g
        # prompt
        g = Adw.PreferencesGroup(description="Copied to the clipboard, then pasted with the chord below.")
        self.prompt_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
        self.prompt_view.add_css_class("prompt-text")
        self.prompt_view.get_buffer().connect("changed", self._on_prompt_text)
        frame = Gtk.ScrolledWindow(min_content_height=140, max_content_height=320, propagate_natural_height=True)
        frame.set_child(self.prompt_view)
        frame.add_css_class("card")
        g.add(frame)
        options = Adw.PreferencesGroup()
        self.submit_row = Adw.SwitchRow(title="Press Enter after pasting")
        self.submit_row.connect("notify::active", lambda r, _p: self._set_prompt_flag("submit", r.get_active()))
        self.restore_row = Adw.SwitchRow(title="Restore the clipboard afterwards")
        self.restore_row.connect("notify::active",
                                 lambda r, _p: self._set_prompt_flag("restoreClipboard", r.get_active()))
        self.paste_row = Adw.ComboRow(title="Paste with", model=Gtk.StringList.new([n for n, _ in PASTE_CHORDS]))
        self.paste_row.connect("notify::selected", self._on_paste_chord)
        for r in (self.submit_row, self.restore_row, self.paste_row):
            options.add(r)
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        wrapper.append(g)
        wrapper.append(options)
        prompt_group = Adw.PreferencesGroup()
        prompt_group.add(wrapper)
        self.forms["prompt"] = prompt_group
        # builtin
        g = Adw.PreferencesGroup()
        self.builtin_row = Adw.ComboRow(title="Brightness",
                                        model=Gtk.StringList.new(["brightness-up", "brightness-down"]))
        self.builtin_row.connect("notify::selected", self._on_builtin_changed)
        g.add(self.builtin_row)
        self.forms["builtin"] = g
        for form in self.forms.values():
            page.add(form)

        advanced = Adw.PreferencesGroup(title="State",
                                        description="Polled by the daemon; exit code 0 shows the key's “on” look.")
        self.state_row = Adw.EntryRow(title="State command")
        self.state_row.connect("changed", lambda r: self._set_field("state", r.get_text()))
        advanced.add(self.state_row)
        page.add(advanced)

        presets = Adw.PreferencesGroup(title="AI coding presets",
                                       description="Replaces this key's look and action in one click.")
        for title, subtitle, fields in AI_PRESETS:
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, f=fields: self.apply_preset(f))
            presets.add(row)
        page.add(presets)
        return page

    def _color_row(self, group, title, field):
        row = Adw.ActionRow(title=title)
        button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        button.connect("notify::rgba", lambda b, _p: self._set_field(field, rgba_to_hex(b.get_rgba())))
        row.add_suffix(button)
        row.set_activatable_widget(button)
        group.add(row)
        return button

    def _install_actions(self):
        def add(name, callback, param=None):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new(param) if param else None)
            action.connect("activate", callback)
            self.add_action(action)
            return action

        add("save", lambda *_: self.save())
        add("page-add", lambda *_: self.add_page())
        add("page-rename", lambda *_: self._rename_page_dialog())
        add("page-left", lambda *_: self.move_page(-1))
        add("page-right", lambda *_: self.move_page(1))
        self.delete_page_action = add("page-delete", lambda *_: self._delete_page_dialog())
        add("key-copy", lambda _a, v: self.copy_key(v.get_int32()), "i")
        self.paste_action = add("key-paste", lambda _a, v: self.paste_key(v.get_int32()), "i")
        add("key-clear", lambda _a, v: self.clear_key(v.get_int32()), "i")
        self.paste_action.set_enabled(False)

    # ----- model access

    def page_keys(self, page_index, pad=False):
        page = self.config["pages"][page_index]
        keys = page.get("keys")
        if not isinstance(keys, list):
            if not pad:
                return [None] * KEY_COUNT
            keys = page["keys"] = []
        if pad and len(keys) < KEY_COUNT:
            keys.extend([None] * (KEY_COUNT - len(keys)))
        return keys

    def get_key(self, page_index, slot):
        keys = self.page_keys(page_index)
        key = keys[slot] if slot < len(keys) else None
        return key if isinstance(key, dict) else None

    def current_key(self):
        return self.get_key(self.page, self.slot)

    def edit(self, fn):
        """Apply fn to the selected key (creating it if the slot is empty), then refresh."""
        keys = self.page_keys(self.page, pad=True)
        if not isinstance(keys[self.slot], dict):
            keys[self.slot] = {}
        key = keys[self.slot]
        convert_legacy(key)
        fn(key)
        if not key:  # e.g. the last field was cleared; an empty slot is null, not {}
            keys[self.slot] = None
        self.changed(self.slot)
        return key

    def changed(self, slot=None):
        if slot is None:
            self.refresh_grid()
        else:
            self.refresh_slot(slot)
        self.update_dirty()

    def is_dirty(self):
        return serialize(self.config) != self.saved_snapshot

    def update_dirty(self):
        dirty = self.is_dirty()
        self.title_widget.set_title(("• " if dirty else "") + "MX Keypad Editor")
        self.title_widget.set_subtitle("Unsaved changes" if dirty else self.config_path)
        self.save_button.set_sensitive(dirty and not self.load_error)

    # ----- loading and saving

    def read_disk(self):
        try:
            with open(self.config_path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def load_from_disk(self, initial=False):
        text = self.read_disk()
        if text is None:
            if initial:
                self.toast(f"{self.config_path} not found; starting a new config")
                self.base_text = ""
            self._after_load()
            return True
        try:
            config = parse_config(text)
        except ValueError as e:
            # Saving now would overwrite whatever is on disk with the editor's stale or empty config,
            # so Save stays off until config.json parses (fixed on disk, then reloaded).
            self.load_error = str(e)
            self.alert("Can't load config.json", f"{e}\n\nSaving is disabled until the file is fixed and reloaded.")
            self.banner.set_title(GLib.markup_escape_text(f"config.json can't be parsed, so saving is off: {e}"))
            self.banner.set_revealed(True)
            if initial:
                self._after_load(keep_banner=True)
            else:
                self.update_dirty()
            return False
        self.load_error = None
        self.config = config
        self.base_text = text
        self.saved_snapshot = serialize(config)
        self._after_load()
        return True

    def _after_load(self, keep_banner=False):
        self.page = min(self.page, len(self.config["pages"]) - 1)
        if not keep_banner:
            self.banner.set_revealed(False)
            self.banner.set_title(CHANGED_BANNER)
        self.rebuild_pages()
        self.refresh_grid()
        self.load_editor()
        self.update_dirty()

    def save(self, then=None):
        if self.load_error:
            self.toast("Saving is off: config.json can't be parsed. Fix it, then reload.")
            return
        disk = self.read_disk()
        if disk is not None and disk != self.base_text:
            dialog = Adw.AlertDialog(heading="Overwrite outside changes?",
                                     body="config.json was changed outside the editor since it was loaded. "
                                          "Saving replaces those changes.")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("overwrite", "Overwrite")
            dialog.set_response_appearance("overwrite", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect("response", lambda _d, r: r == "overwrite" and self._write(then))
            dialog.present(self)
            return
        self._write(then)

    def _write(self, then=None):
        text = serialize(self.config)
        tmp = os.path.join(self.config_dir, ".config.json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.config_path)
        except OSError as e:
            self.alert("Save failed", str(e))
            return False
        self.base_text = text
        self.saved_snapshot = text
        self.banner.set_revealed(False)
        self.banner.set_title(CHANGED_BANNER)
        self.update_dirty()
        self.toast("Saved")
        if then:
            then()
        return True

    def _watch_config(self):
        directory = Gio.File.new_for_path(self.config_dir)
        self.monitor = directory.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.monitor.connect("changed", self._on_dir_changed)

    def _on_dir_changed(self, _monitor, file, other, _event):
        names = {f.get_basename() for f in (file, other) if f is not None}
        if os.path.basename(self.config_path) not in names:
            return
        if self.monitor_timer:
            GLib.source_remove(self.monitor_timer)
        self.monitor_timer = GLib.timeout_add(300, self._check_external_change)

    def _check_external_change(self):
        self.monitor_timer = 0
        disk = self.read_disk()
        if disk is None or disk == self.base_text:
            return GLib.SOURCE_REMOVE
        if self.is_dirty():  # never drop unsaved edits; the banner offers Reload
            if not self.load_error:
                self.banner.set_title(CHANGED_BANNER)
            self.banner.set_revealed(True)
        elif self.load_from_disk():
            self.toast("Reloaded config.json (changed outside the editor)")
        return GLib.SOURCE_REMOVE

    def _confirm_reload(self):
        dialog = Adw.AlertDialog(heading="Reload config.json?", body="Your unsaved changes will be lost.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("reload", "Reload")
        dialog.set_response_appearance("reload", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, r: r == "reload" and self.load_from_disk())
        dialog.present(self)

    def _on_close_request(self, *_):
        if self.force_close or not self.is_dirty():
            self.render.shutdown()
            return False
        dialog = Adw.AlertDialog(heading="Save changes?", body="config.json has unsaved changes.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def respond(_d, response):
            if response == "discard":
                self._close_now()
            elif response == "save":
                self.save(then=self._close_now)

        dialog.connect("response", respond)
        dialog.present(self)
        return True

    def _close_now(self):
        self.force_close = True
        self.close()

    # ----- pages

    def rebuild_pages(self):
        self.loading = True
        names = [str(p.get("name") or f"Page {i + 1}") for i, p in enumerate(self.config["pages"])]
        self.page_dropdown.set_model(Gtk.StringList.new(names))
        self.page_dropdown.set_selected(self.page)
        self.loading = False
        self.delete_page_action.set_enabled(len(self.config["pages"]) > 1)

    def _on_page_selected(self, dropdown, _pspec):
        if self.loading:
            return
        selected = dropdown.get_selected()
        if selected != Gtk.INVALID_LIST_POSITION and selected != self.page:
            self.show_page(selected)

    def show_page(self, index):
        self.page = index
        self.render.forget()
        self.refresh_grid()
        self.load_editor()

    def add_page(self):
        self.config["pages"].insert(self.page + 1, {"name": f"Page {len(self.config['pages']) + 1}",
                                                    "keys": [None] * KEY_COUNT})
        self.page += 1
        self.rebuild_pages()
        self.show_page(self.page)
        self.update_dirty()

    def rename_page(self, name):
        self.config["pages"][self.page]["name"] = name
        self.rebuild_pages()
        self.update_dirty()

    def _rename_page_dialog(self):
        entry = Gtk.Entry(text=self.config["pages"][self.page].get("name") or "", activates_default=True)
        dialog = Adw.AlertDialog(heading="Rename page", extra_child=entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.connect("response", lambda _d, r: r == "rename" and entry.get_text().strip()
                       and self.rename_page(entry.get_text().strip()))
        dialog.present(self)

    def delete_page(self):
        if len(self.config["pages"]) <= 1:
            self.toast("The last page can't be deleted")
            return
        del self.config["pages"][self.page]
        self.page = min(self.page, len(self.config["pages"]) - 1)
        self.rebuild_pages()
        self.show_page(self.page)
        self.update_dirty()

    def _delete_page_dialog(self):
        if len(self.config["pages"]) <= 1:
            self.toast("The last page can't be deleted")
            return
        name = self.config["pages"][self.page].get("name") or f"Page {self.page + 1}"
        dialog = Adw.AlertDialog(heading=f"Delete “{name}”?", body="Its keys are removed when you save.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, r: r == "delete" and self.delete_page())
        dialog.present(self)

    def move_page(self, delta):
        pages = self.config["pages"]
        target = self.page + delta
        if not 0 <= target < len(pages):
            return
        pages[self.page], pages[target] = pages[target], pages[self.page]
        self.page = target
        self.rebuild_pages()
        self.update_dirty()

    # ----- grid

    def refresh_grid(self):
        self.refresh_css()
        for slot in range(KEY_COUNT):
            self.refresh_slot(slot, css=False)

    def refresh_slot(self, slot, css=True):
        if css:
            self.refresh_css()
        key = self.get_key(self.page, slot)
        button = self.buttons[slot]
        button.show_key(key, self.config_dir)
        if slot == self.slot:
            button.add_css_class("selected")
        else:
            button.remove_css_class("selected")
        if key:
            self.render.request(slot, key)
        else:
            self.render.cancel_slot(slot)

    def refresh_css(self):
        rules = [CSS]
        for slot in range(KEY_COUNT):
            key = self.get_key(self.page, slot) or {}
            bg = key.get("bg") if re.fullmatch(r"#[0-9a-fA-F]{6}", str(key.get("bg", ""))) else DEFAULT_BG
            fg = key.get("fg") if re.fullmatch(r"#[0-9a-fA-F]{6}", str(key.get("fg", ""))) else DEFAULT_FG
            if key:
                rules.append(f".mxkey-{slot} {{ background: {bg}; color: {fg}; }}")
        self.css.load_from_string("\n".join(rules))

    def _on_render(self, slot, png):
        self.buttons[slot].show_png(png)

    def select_slot(self, slot):
        old, self.slot = self.slot, slot
        self.buttons[old].remove_css_class("selected")
        self.buttons[slot].add_css_class("selected")
        self.load_editor()

    def swap_from_payload(self, payload, page, slot):
        """Handle a drop of "p:k" onto (page, slot); returns False for anything malformed."""
        m = re.fullmatch(r"(\d{1,4}):(\d)", payload) if isinstance(payload, str) else None
        if not m:
            return False
        src_page, src_slot = int(m.group(1)), int(m.group(2))
        if src_page >= len(self.config["pages"]) or src_slot >= KEY_COUNT or (src_page, src_slot) == (page, slot):
            return False
        a, b = self.page_keys(src_page, pad=True), self.page_keys(page, pad=True)
        a[src_slot], b[slot] = b[slot], a[src_slot]
        self.slot = slot
        self.changed()
        self.load_editor()
        return True

    def copy_key(self, slot):
        key = self.get_key(self.page, slot)
        self.key_clipboard = copy.deepcopy(key)
        self.paste_action.set_enabled(key is not None)
        self.toast("Key copied" if key else "Nothing to copy")

    def paste_key(self, slot):
        if self.key_clipboard is None:
            return
        self.page_keys(self.page, pad=True)[slot] = copy.deepcopy(self.key_clipboard)
        self.changed(slot)
        self.select_slot(slot)

    def clear_key(self, slot):
        keys = self.page_keys(self.page, pad=True)
        keys[slot] = None
        self.changed(slot)
        if slot == self.slot:
            self.load_editor()

    # ----- editor pane

    def load_editor(self, ftype=None):
        """Fill the editor from the selected key without writing anything back.

        `ftype` shows that form instead of the detected one (e.g. "keys" for a media-detected chord)."""
        key = self.current_key() or {}
        action = key_action(key) or {}
        self.loading = True
        try:
            name = str(self.config["pages"][self.page].get("name") or f"Page {self.page + 1}")
            self.slot_title.set_title(f"Key {self.slot + 1} on {GLib.markup_escape_text(name)}")
            self.label_row.set_text(str(key.get("label") or ""))
            self.sub_row.set_text(str(key.get("sub") or ""))
            self.bg_button.set_rgba(hex_to_rgba(key.get("bg"), DEFAULT_BG))
            self.fg_button.set_rgba(hex_to_rgba(key.get("fg"), DEFAULT_FG))
            self.state_row.set_text(str(key.get("state") or ""))
            self._load_icon_row(key)

            ftype = ftype or form_type(action)
            self.type_row.set_selected(TYPE_IDS.index(ftype))
            self._show_form(ftype)
            self.command_row.set_text(action.get("command", "") if action.get("type") == "command" else "")
            self._load_app_row(action.get("desktopId") if action.get("type") == "app" else None)
            preset = media_preset_index(action)
            for i, check in enumerate(self.media_checks):
                check.set_visible(i == preset)
            codes = action.get("codes") if action.get("type") == "keys" else []
            self._load_codes(codes if isinstance(codes, list) else [])
            prompt = action if action.get("type") == "prompt" else {}
            self.prompt_view.get_buffer().set_text(prompt.get("text") or "")
            self.submit_row.set_active(prompt.get("submit") is True)
            self.restore_row.set_active(prompt.get("restoreClipboard") is True)
            paste = prompt.get("pasteCodes", KITTY_PASTE)
            self.paste_row.set_selected(1 if paste == CTRL_V_PASTE else 0)
            self.builtin_row.set_selected(1 if action.get("name") == "brightness-down" else 0)
            self.test_button.set_sensitive(ftype not in ("none", "builtin")
                                           and (ftype == "media" or action.get("type") == ftype))
        finally:
            self.loading = False

    def _show_form(self, ftype):
        for name, form in self.forms.items():
            form.set_visible(name == ftype)

    def _load_icon_row(self, key):
        icon = key.get("icon")
        path = os.path.join(self.config_dir, icon) if icon else None
        self.icon_row.set_subtitle(GLib.markup_escape_text(icon) if icon else "None")
        if path and os.path.exists(path):
            self.icon_preview.set_from_file(path)
        else:
            self.icon_preview.set_from_icon_name("image-missing-symbolic" if icon else None)
        action = key_action(key) or {}
        self.app_icon_button.set_sensitive(action.get("type") == "app" and bool(action.get("desktopId")))

    def _load_app_row(self, desktop_id):
        app = find_app(desktop_id)
        if app:
            self.app_row.set_title(GLib.markup_escape_text(app.get_display_name()))
            self.app_row.set_subtitle(GLib.markup_escape_text(desktop_id))
            self.app_image.set_from_gicon(app.get_icon()) if app.get_icon() else self.app_image.clear()
        else:
            self.app_row.set_title("App")
            self.app_row.set_subtitle(GLib.markup_escape_text(f"{desktop_id} (not installed)")
                                      if desktop_id else "None chosen")
            self.app_image.set_from_icon_name("application-x-executable-symbolic")

    def _load_codes(self, codes):
        self.chord_row.set_subtitle(GLib.markup_escape_text(chord_text(codes)))
        self.codes_row.set_text(", ".join(str(c) for c in codes))
        self.codes_row.remove_css_class("error")

    def _set_field(self, field, value):
        if self.loading:
            return
        self.edit(lambda key: key.__setitem__(field, value) if value else key.pop(field, None))

    def _set_action(self, action):
        def apply(key):
            if action:
                key["action"] = action
            else:
                key.pop("action", None)
        self.edit(apply)

    def _set_action_field(self, field, value):
        if self.loading:
            return
        key = self.current_key()
        action = key_action(key) if key else None
        if action is None or action.get("type") != {"command": "command", "desktopId": "app",
                                                     "codes": "keys", "text": "prompt", "name": "builtin"}[field]:
            return
        self.edit(lambda k: k["action"].__setitem__(field, value))

    def _on_type_changed(self, row, _pspec):
        if self.loading:
            return
        ftype = TYPE_IDS[row.get_selected()]
        self._show_form(ftype)
        if ftype == "media":  # the action changes once a preset is picked
            return
        current = key_action(self.current_key()) or {}
        # Compare raw types, so a media-detected command/chord keeps its fields under its own form.
        if ftype == "none":
            if current:
                self._set_action(None)
        elif current.get("type") != ftype:
            defaults = {
                "command": {"type": "command", "command": ""},
                "app": {"type": "app", "desktopId": ""},
                "prompt": {"type": "prompt", "text": "", "submit": False, "restoreClipboard": False},
                "builtin": {"type": "builtin", "name": "brightness-up"},
            }
            # An empty chord is invalid, so keys keeps the old action until something is recorded.
            if ftype in defaults:
                self._set_action(copy.deepcopy(defaults[ftype]))
        self.load_editor(ftype)

    def _app_defaults(self, app):
        """(label, icon path) a key gets from an app; the icon path is computed without copying anything."""
        if not app:
            return None, None
        label = (app.get_display_name() or app.get_id())[:10]
        try:
            file = app_icon_file(app, self)
            _ok, data, _etag = file.load_contents(None)
            ext = sniff_image_ext(bytes(data), file.get_basename() or "")
            icon = f"icons/{hashlib.sha1(bytes(data)).hexdigest()}.{ext}" if ext else None
        except (AttributeError, GLib.Error):
            icon = None
        return label, icon

    def _on_app_chosen(self, app):
        # Label and icon follow the app unless the user customised them.
        key = self.current_key() or {}
        old_label, old_icon = self._app_defaults(find_app((key_action(key) or {}).get("desktopId")))
        follow_label = not key.get("label") or key.get("label") == old_label
        follow_icon = not key.get("icon") or key.get("icon") == old_icon
        label = self._app_defaults(app)[0]

        def apply(k):
            k["action"] = {"type": "app", "desktopId": app.get_id()}
            if follow_label:
                k["label"] = label
        self.edit(apply)
        if follow_icon:
            self._clear_icon()
            self._use_app_icon(quiet=True)
        self.load_editor()

    def _set_codes(self, codes):
        def apply(key):
            if (key.get("action") or {}).get("type") != "keys":
                key["action"] = {"type": "keys"}
            key["action"]["codes"] = codes
        self.edit(apply)
        self.load_editor()

    def _on_codes_typed(self, row):
        if self.loading:
            return
        parts = [p.strip() for p in row.get_text().replace(" ", ",").split(",") if p.strip()]
        if not all(p.isdigit() and 0 < int(p) < 768 for p in parts):
            row.add_css_class("error")
            return
        row.remove_css_class("error")
        codes = [int(p) for p in parts]
        if not codes:  # never store an empty chord; the previous action stays
            return
        if (key_action(self.current_key()) or {}).get("type") == "keys":
            self._set_action_field("codes", codes)
        else:
            self.edit(lambda k: k.__setitem__("action", {"type": "keys", "codes": codes}))
            self.test_button.set_sensitive(True)
        self.chord_row.set_subtitle(GLib.markup_escape_text(chord_text(codes)))

    def _on_prompt_text(self, buffer):
        if self.loading:
            return
        self._set_action_field("text", buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False))

    def _set_prompt_flag(self, field, active):
        if self.loading:
            return
        key = self.current_key()
        if not key or (key.get("action") or {}).get("type") != "prompt":
            return
        # Written explicitly either way, so the daemon's defaults never decide what the switch meant.
        self.edit(lambda k: k["action"].__setitem__(field, active))

    def _on_paste_chord(self, row, _pspec):
        if self.loading:
            return
        key = self.current_key()
        if not key or (key.get("action") or {}).get("type") != "prompt":
            return
        codes = PASTE_CHORDS[row.get_selected()][1]
        self.edit(lambda k: k["action"].pop("pasteCodes", None) if codes == KITTY_PASTE
                  else k["action"].__setitem__("pasteCodes", list(codes)))

    def _on_builtin_changed(self, row, _pspec):
        if self.loading:
            return
        self._set_action_field("name", ["brightness-up", "brightness-down"][row.get_selected()])

    def apply_media_preset(self, index):
        _title, label, action, state = MEDIA_PRESETS[index]

        def apply(key):
            key["action"] = copy.deepcopy(action)
            if state:
                key["state"] = state
                key["on"] = dict(MUTED_ON)
            elif key.get("state") in MEDIA_STATES:
                key.pop("state", None)
                key.pop("on", None)
            if not key.get("label"):
                key["label"] = label
                key.setdefault("bg", "#3d3d3d")
        self.edit(apply)
        self.load_editor()

    def apply_preset(self, fields):
        def apply(key):
            for name in ("label", "sub", "bg", "fg", "icon", "action", "state", "on"):
                key.pop(name, None)
            key.update(copy.deepcopy(fields))
        self.edit(apply)
        self.load_editor()
        if fields["action"]["type"] == "prompt":
            self.prompt_view.grab_focus()

    # ----- icons

    def _pick_icon_file(self, *_):
        dialog = Gtk.FileDialog(title="Choose an icon")
        images = Gtk.FileFilter(name="PNG or SVG images")
        images.add_mime_type("image/png")
        images.add_mime_type("image/svg+xml")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(images)
        dialog.set_filters(filters)
        dialog.set_default_filter(images)
        dialog.open(self, None, self._on_icon_file)

    def _on_icon_file(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # cancelled
        if file:
            self._store_icon_from(file)

    def _store_icon_from(self, file, quiet=False):
        try:
            _ok, data, _etag = file.load_contents(None)
            rel = store_icon(self.config_dir, bytes(data), file.get_basename() or "")
        except (GLib.Error, OSError, ValueError) as e:
            if not quiet:
                self.toast(f"Couldn't use that icon: {getattr(e, 'message', e)}")
            return False
        self.edit(lambda key: key.__setitem__("icon", rel))
        self.load_editor()
        return True

    def _use_app_icon(self, *_, quiet=False):
        action = key_action(self.current_key()) or {}
        file = app_icon_file(find_app(action.get("desktopId")), self)
        if file is None:
            if not quiet:
                self.toast("That app has no icon file")
            return
        self._store_icon_from(file, quiet=quiet)

    def _clear_icon(self, *_):
        if (self.current_key() or {}).get("icon"):
            self.edit(lambda key: key.pop("icon", None))
            self.load_editor()

    # ----- testing actions

    def test_action(self):
        action = key_action(self.current_key())
        if not action:
            self.toast("This key has no action")
            return
        if action.get("type") == "builtin":
            self.toast("Brightness actions only work on the keypad")
            return
        node, script = find_node(), os.path.join(REPO_DIR, "run-action.js")
        if not node or not os.path.exists(script):
            self.alert("Can't test", "Needs node and run-action.js "
                                     f"(node: {node or 'not found'}, script: {script})")
            return
        payload = json.dumps(action, ensure_ascii=False)
        if action.get("type") in ("keys", "prompt"):
            self._countdown(3, Adw.Toast(title="", timeout=0), lambda: self._run_action(node, script, payload))
        else:
            self._run_action(node, script, payload)

    def _countdown(self, seconds, toast, then):
        if seconds == 0:
            toast.dismiss()
            then()
            return GLib.SOURCE_REMOVE
        toast.set_title(f"Running in {seconds}… focus the target window")
        if seconds == 3:
            self.toasts.add_toast(toast)
        GLib.timeout_add(1000, self._countdown, seconds - 1, toast, then)
        return GLib.SOURCE_REMOVE

    def _run_action(self, node, script, payload):
        try:
            launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDERR_PIPE
                                                  | Gio.SubprocessFlags.STDOUT_SILENCE)
            launcher.set_cwd(REPO_DIR)
            proc = launcher.spawnv([node, script])
        except GLib.Error as e:
            self.alert("Test failed", e.message)
            return
        proc.communicate_utf8_async(payload, None, self._on_action_done)

    def _on_action_done(self, proc, result):
        try:
            _ok, _out, err = proc.communicate_utf8_finish(result)
        except GLib.Error as e:
            self.alert("Test failed", e.message)
            return
        if proc.get_successful():
            self.toast("Action ran")
        else:
            self.alert("Test failed", (err or "").strip() or f"run-action.js exited with {proc.get_exit_status()}")

    # ----- feedback

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text)))

    def alert(self, heading, body):
        if getattr(self, "self_test", False):
            print(f"{heading}: {body}", file=sys.stderr)
            return
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self)


# ---------------------------------------------------------------- self test

def run_self_test(win):
    """Exercise load -> edit -> serialize on the in-memory config. Never writes config.json."""
    before = win.read_disk()
    win.self_test = True
    failures = []

    def check(cond, what):
        if not cond:
            failures.append(what)

    config = win.config
    config["zzTopLevelUnknown"] = {"keep": True}
    for p in range(len(config["pages"])):
        win.show_page(p)
        for s in range(KEY_COUNT):
            win.select_slot(s)

    win.show_page(0)
    win.select_slot(0)
    win.edit(lambda k: k.__setitem__("zzKeyUnknown", 42))
    win.label_row.set_text("Edited")
    key = win.current_key()
    check(key.get("label") == "Edited", "label edit reaches the model")
    check("run" not in key and "builtin" not in key and key.get("action"), "legacy key converted on edit")

    rgba = Gdk.RGBA()
    rgba.parse("#123456")
    win.bg_button.set_rgba(rgba)
    check(win.current_key().get("bg") == "#123456", "colour edit reaches the model")
    win.loading = True
    win.bg_button.set_rgba(hex_to_rgba("#abcdef", DEFAULT_BG))
    win.loading = False
    check(win.current_key().get("bg") == "#123456", "colour set while loading is not written back")

    win.type_row.set_selected(TYPE_IDS.index("prompt"))
    win.prompt_view.get_buffer().set_text("Review the diff\nand list bugs")
    win.submit_row.set_active(True)
    win.paste_row.set_selected(1)
    action = win.current_key()["action"]
    check(action == {"type": "prompt", "text": "Review the diff\nand list bugs", "submit": True,
                     "restoreClipboard": False, "pasteCodes": CTRL_V_PASTE}, f"prompt action shape: {action}")
    win.paste_row.set_selected(0)
    win.submit_row.set_active(False)
    check("pasteCodes" not in win.current_key()["action"] and win.current_key()["action"]["submit"] is False,
          "kitty paste chord omitted, submit written explicitly")

    win.select_slot(1)
    win._set_codes([29, 42, 20])
    check(win.current_key()["action"] == {"type": "keys", "codes": [29, 42, 20]}, "recorded codes stored")
    check(chord_text([29, 42, 20]) == "Ctrl+Shift+T", "chord text")
    win.apply_media_preset(5)
    check(win.current_key().get("state", "").startswith("wpctl") and win.current_key()["on"] == MUTED_ON,
          "mute preset sets state")
    win.apply_media_preset(0)
    check("state" not in win.current_key(), "play preset drops the mute state")
    check(form_type(win.current_key()["action"]) == "media", "media preset detected")
    for _t, _s, fields in AI_PRESETS:
        win.apply_preset(fields)
        check(win.current_key()["action"] == fields["action"], f"preset {fields['label']}")
    win.type_row.set_selected(TYPE_IDS.index("command"))
    win.command_row.set_text("echo hi")
    check(win.current_key()["action"] == {"type": "command", "command": "echo hi"}, "command form")
    win.type_row.set_selected(TYPE_IDS.index("none"))
    check("action" not in win.current_key(), "type none clears the action")

    # Type switches keep a media-detected action's fields and never store an empty chord.
    win.select_slot(2)
    win.apply_media_preset(0)
    win.type_row.set_selected(TYPE_IDS.index("keys"))
    check(win.current_key()["action"] == {"type": "keys", "codes": [164]}, "media chord kept under Keyboard shortcut")
    check(win.type_row.get_selected() == TYPE_IDS.index("keys"), "Keyboard shortcut stays selected")
    win.apply_media_preset(6)
    win.type_row.set_selected(TYPE_IDS.index("command"))
    check(win.current_key()["action"] == MEDIA_PRESETS[6][2], "media command kept under Shell command")
    check(win.command_row.get_text() == MEDIA_PRESETS[6][2]["command"], "command row shows the kept command")
    win.type_row.set_selected(TYPE_IDS.index("prompt"))
    win.type_row.set_selected(TYPE_IDS.index("keys"))
    check(win.current_key()["action"]["type"] == "prompt", "switching to keys leaves the old action until recorded")
    win.codes_row.set_text("")
    check(win.current_key()["action"]["type"] == "prompt", "empty codes never written")
    win.codes_row.set_text("29, 46")
    check(win.current_key()["action"] == {"type": "keys", "codes": [29, 46]}, "typed codes create a keys action")

    # A key emptied field by field becomes null, and its pending render is dropped.
    win.clear_key(2)
    win.label_row.set_text("x")
    win.label_row.set_text("")
    check(win.page_keys(0)[2] is None, "emptied key stored as null")
    win.render.request(3, {"label": "late"})
    win.render.cancel_slot(3)
    check(3 not in win.render.timers and 3 not in win.render.wanted and 3 not in win.render.latest,
          "cancel_slot drops pending render work")
    win.select_slot(1)

    # The recorder ignores auto-repeat and only restarts once the finished chord is fully released.
    recorded = []
    rec = ShortcutRecorder(win, recorded.append)
    press = lambda c: rec._on_press(None, 0, c + 8, 0)
    release = lambda c: rec._on_release(None, 0, c + 8, 0)
    press(29), press(29), press(46), press(46)
    release(46)
    check(rec.codes == [29, 46] and rec.finished, f"chord recorded once despite repeats: {rec.codes}")
    press(29), press(30)
    check(rec.codes == [29, 46], "presses while the chord is still held don't restart it")
    release(29), release(30)
    press(30)
    check(rec.codes == [30] and not rec.finished, "a new key after full release starts a new chord")
    rec.destroy()

    # A config.json that doesn't parse turns Save off instead of letting it be overwritten.
    real_path = win.config_path
    with tempfile.TemporaryDirectory() as tmp:
        bad = os.path.join(tmp, "config.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{ not json")
        win.config_path = bad
        check(not win.load_from_disk(), "broken config not loaded")
        check(win.load_error and not win.save_button.get_sensitive(), "Save disabled after a parse error")
        win.save()
        with open(bad, encoding="utf-8") as f:
            check(f.read() == "{ not json", "broken config.json not overwritten")
    win.config_path = real_path
    win.load_error = None
    win.update_dirty()

    first, second = copy.deepcopy(win.get_key(0, 0)), copy.deepcopy(win.get_key(0, 1))
    check(win.swap_from_payload("0:0", 0, 1), "swap accepted")
    check(win.get_key(0, 1) == first and win.get_key(0, 0) == second, "swap result")
    for bad in ("x", "0:9", "99:0", "0:1", "-1:0", "0:0;rm", None):
        check(not win.swap_from_payload(bad, 0, 1), f"bad payload rejected: {bad!r}")
    win.copy_key(1)
    win.paste_key(8)
    check(win.get_key(0, 8) == win.get_key(0, 1) and win.get_key(0, 8) is not win.get_key(0, 1), "copy/paste")
    win.clear_key(8)
    check(win.get_key(0, 8) is None, "clear")

    pages = len(config["pages"])
    win.add_page()
    win.rename_page("Temp")
    win.move_page(-1)
    check(config["pages"][win.page]["name"] == "Temp", "page add/rename/move")
    win.delete_page()
    check(len(config["pages"]) == pages, "page delete")
    while len(config["pages"]) > 1:
        win.delete_page()
    win.delete_page()
    check(len(config["pages"]) == 1, "last page kept")

    round_trip = json.loads(serialize(config))
    check(round_trip == config, "serialize round-trip")
    check(round_trip.get("zzTopLevelUnknown") == {"keep": True}, "unknown top-level field kept")
    check(win.is_dirty(), "dirty after edits")
    check(win.read_disk() == before, "config.json untouched")

    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    print("self-test " + ("failed" if failures else "passed"), file=sys.stderr)
    return 1 if failures else 0


# ---------------------------------------------------------------- application

class EditorApp(Adw.Application):
    def __init__(self, config_path, self_test=False):
        flags = Gio.ApplicationFlags.NON_UNIQUE if self_test else Gio.ApplicationFlags.DEFAULT_FLAGS
        super().__init__(application_id=APP_ID, flags=flags)
        self.config_path = config_path
        self.self_test = self_test
        self.exit_code = 0
        self.window = None
        self.set_accels_for_action("win.save", ["<Control>s"])

    def do_activate(self):
        if self.window is None:
            self.window = EditorWindow(self, self.config_path, render_enabled=not self.self_test)
        if self.self_test:
            self.exit_code = run_self_test(self.window)
            self.window.force_close = True
            self.window.close()
            self.quit()
            return
        self.window.present()


def main():
    parser = argparse.ArgumentParser(description="Edit the MX Keypad's config.json")
    parser.add_argument("--config", help="path to config.json (default: the repo's config.json)")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    path = os.path.realpath(args.config or os.path.join(REPO_DIR, "config.json"))
    if os.path.isdir(path):
        path = os.path.join(path, "config.json")
    app = EditorApp(path, self_test=args.self_test)
    status = app.run([sys.argv[0]])
    return app.exit_code or status


if __name__ == "__main__":
    sys.exit(main())
