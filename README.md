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

`config.json` holds a list of pages. Each page holds up to 9 keys, numbered left to right and top to
bottom. Each key can have these fields:

| Field     | Meaning                                                                   |
|-----------|---------------------------------------------------------------------------|
| `label`   | Large text on the key                                                     |
| `sub`     | Smaller second line                                                       |
| `bg`/`fg` | Background and text colours                                               |
| `run`     | Shell command run on press                                                |
| `builtin` | `brightness-up` or `brightness-down`, to change the keypad's brightness   |
| `state`   | Command polled every 1.5 s; exit code 0 means the key is "on"             |
| `on`      | Fields that replace the key's normal fields while it is "on"              |

The daemon reloads `config.json` when the file changes. Logs are in
`journalctl --user -u mx-keypad -f`.
