# mx-keypad

A daemon for the Logitech MX Creative Keypad on Linux (Ubuntu 24.04, GNOME on Wayland). It draws
labelled keys from `config.json`, runs each key's shell command when the key is pressed, and uses
the two page buttons to move between pages. It uses
[`@logitech-mx-creative-console/node`](https://github.com/julusian/node-logitech-mx-creative-console)
to talk to the device.

## Setup

1. Give the desktop user access to the keypad's hidraw node:

   ```bash
   sudo cp 50-logi-mx-creative-console.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules
   sudo udevadm trigger --action=change --subsystem-match=hidraw
   ```

2. Install the dependencies: `npm install`. The prebuilt binaries for `node-hid` and `jpeg-turbo`
   load without their install scripts. If a Node upgrade breaks them, run `npm rebuild`.

3. Install the user services:

   ```bash
   cp systemd/*.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now ydotoold mx-keypad
   ```

   `systemd/mx-keypad.service` starts Node from the nvm path for v24.21.0. Edit `ExecStart` if
   that version changes.

## ydotool

The media and volume keys send keycodes through `ydotool`. Ubuntu's apt package is version 0.1.8,
which expects key names and types a `1` when given `115:1 115:0`. This setup uses v1.0.4 built
from source into `~/.local/bin`:

```bash
git clone --depth 1 --branch v1.0.4 https://github.com/ReimuNotMoe/ydotool.git
cmake -S ydotool -B ydotool/build -DCMAKE_BUILD_TYPE=Release
cmake --build ydotool/build --target ydotool ydotoold
install -m 755 ydotool/build/ydotool ydotool/build/ydotoold ~/.local/bin/
```

The build of the `all` target fails at the man pages unless `scdoc` is installed, so the commands
above build the two binaries only. `systemd/ydotoold.service` replaces the apt package's user unit
of the same name, and the daemon opens `/dev/uinput` through the `input` group.

## config.json

`config.json` holds an optional `brightness` (10 to 100) and a list of pages. Each page has a `name`
and up to 9 keys, numbered left to right and top to bottom. A `null` key is blank. Each key can have
these fields:

| Field     | Meaning                                                                   |
|-----------|---------------------------------------------------------------------------|
| `label`   | Large text on the key                                                     |
| `sub`     | Smaller second line                                                       |
| `bg`/`fg` | Background and text colours, as `#rrggbb`                                 |
| `icon`    | `icons/<sha1>.png` or `.svg`, relative to the config directory, drawn above the label |
| `action`  | What a press does; see below                                              |
| `state`   | Command polled every 1.5 s; exit code 0 means the key is "on"             |
| `on`      | Fields that replace the key's normal fields while it is "on"              |

`action` is one of:

| `type`    | Other fields                                    | Effect                                              |
|-----------|-------------------------------------------------|-----------------------------------------------------|
| `command` | `command`                                       | Runs a shell command                                |
| `app`     | `desktopId`, e.g. `firefox_firefox.desktop`     | Launches the app with `gtk-launch`                  |
| `keys`    | `codes`: evdev keycodes, e.g. `[29, 42, 20]`    | Presses the keys in order, releases them in reverse |
| `prompt`  | `text`, `submit`, `pasteCodes`, `restoreClipboard` | Copies `text` to the clipboard, pastes it, and presses Enter if `submit` is true |
| `builtin` | `name`: `brightness-up` or `brightness-down`     | Changes the keypad's brightness                     |

`pasteCodes` defaults to `[29, 42, 47]` (Ctrl+Shift+V, for kitty); use `[29, 47]` for Ctrl+V.
`submit` and `restoreClipboard` default to `false`.

Older keys may use the legacy fields instead of `action`: `run` (a shell command) and `builtin`
(`brightness-up` or `brightness-down`). The daemon still reads them, with the precedence
`action` > `builtin` > `run`. When the editor saves a key you have edited, it replaces them with an
`action`. Fields the daemon doesn't know are kept.

The daemon reloads `config.json` when the file changes. Logs are in
`journalctl --user -u mx-keypad -f`.

## Editor

`app/mx_keypad_editor.py` is a GTK 4 / libadwaita app for editing `config.json`. It needs only the
system Python and GTK packages that Ubuntu 24.04 ships with GNOME. Install its launcher, then open
**MX Keypad Editor** from the app grid:

```bash
cp app/mx-keypad-editor.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications
```

The `.desktop` file holds absolute paths to this checkout; edit `Exec` if the repo moves. You can
also run `python3 app/mx_keypad_editor.py`, and `--config <path>` edits a different config file.

- The header bar picks the page. Its menu adds, renames, moves and deletes pages. The last page
  can't be deleted.
- The 3x3 grid shows each key as the keypad draws it. Click a key to edit it, drag one key onto
  another to swap them, and right-click to copy, paste or clear a key.
- The editor pane sets the label, sub-label, colours, icon and action. Icons come from a PNG or SVG
  file or from the chosen app's icon, and are copied to `icons/` next to `config.json`.
- Action types: shell command; app, chosen from the installed apps; media and volume presets,
  including speaker and mic mute keys that show "MUTED"; keyboard shortcut, recorded by pressing
  it; prompt; and keypad brightness.
- **AI coding presets** set up an OpenWhispr dictation or voice-agent key, Claude Code Esc or
  Shift+Tab, or a new prompt key in one click.
- **Test** runs the selected key's action now. Shortcut and prompt actions wait 3 seconds first so
  you can focus the target window.
- **Save** (Ctrl+S) writes `config.json` atomically, and the daemon picks it up within a second.
  The title shows when there are unsaved changes, and closing asks before discarding them. If
  `config.json` changes on disk while the editor is open, it reloads, or offers to when you have
  unsaved changes.

Previews and **Test** run `render-server.js` and `run-action.js` with Node. The editor looks for
Node in `$MX_KEYPAD_NODE`, then on `PATH`, then the newest `~/.nvm/versions/node/*/bin/node`. Without
Node, keys show as plain coloured labels.
