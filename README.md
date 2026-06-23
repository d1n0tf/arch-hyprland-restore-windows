# Hypr Restore Windows

A small Hyprland user service that remembers a window's workspace, floating mode, and geometry by class name, then tries to restore that saved state when a matching window opens again.

The script also exposes a live control socket, so exclusions and saved window state can be inspected and changed without restarting the systemd service.

## Files

The project is designed to keep everything in one directory:

```text
~/.config/systemd/user/
  hypr-restore-windows.py
  hypr-window-memory.service
  hypr-restore-windows.config.json
  hypr-restore-windows.state.json
  hypr-restore-windows.control.sock
```

File roles:

- `hypr-restore-windows.py` - main script and CLI.
- `hypr-window-memory.service` - systemd user unit.
- `hypr-restore-windows.config.json` - dynamic exclusions.
- `hypr-restore-windows.state.json` - saved window state.
- `hypr-restore-windows.control.sock` - runtime socket created automatically by the running service.

## Startup

The unit expects the script at this path:

```text
%h/.config/systemd/user/hypr-restore-windows.py
```

After changing the unit file or copying the files for the first time:

```bash
systemctl --user daemon-reload
systemctl --user enable --now hypr-window-memory.service
```

Check the service:

```bash
systemctl --user status hypr-window-memory.service --no-pager
./hypr-restore-windows.py status
```

If the service is already enabled and only needs a restart:

```bash
systemctl --user restart hypr-window-memory.service
```

## Live Control

The commands below talk to the running service through `hypr-restore-windows.control.sock`.

General status:

```bash
./hypr-restore-windows.py status
./hypr-restore-windows.py clients
./hypr-restore-windows.py sync
./hypr-restore-windows.py reload
```

Exclusions:

```bash
./hypr-restore-windows.py exclude list
./hypr-restore-windows.py exclude add firefox
./hypr-restore-windows.py exclude remove firefox
./hypr-restore-windows.py exclude clear
```

`ignore` can be used as an alias for `exclude`:

```bash
./hypr-restore-windows.py ignore list
```

Saved state:

```bash
./hypr-restore-windows.py state list
./hypr-restore-windows.py state show firefox
./hypr-restore-windows.py state forget firefox
./hypr-restore-windows.py state clear --force
```

## How It Works

The service listens to Hyprland socket2 events. When a window opens, it looks up saved state by class name and applies it through `hyprctl`.

On move, workspace, floating/fullscreen events, and during the periodic fallback poll, the service updates `hypr-restore-windows.state.json`.

There are two kinds of exclusions:

- built-in exclusions from the script, such as `waybar`, `mako`, `wofi`, and `steam`;
- dynamic exclusions saved in `hypr-restore-windows.config.json`.

Built-in exclusions cannot be removed through the CLI. Dynamic exclusions can be added and removed at runtime.

## Git

Usually worth committing:

- `README.md`
- `hypr-restore-windows.py`
- `hypr-window-memory.service`
- `hypr-restore-windows.config.json`, if you want to version your exclusions

Usually not worth committing:

- `hypr-restore-windows.control.sock`
- `hypr-restore-windows.state.json`
- `__pycache__/`
- `*.pyc`

Suggested `.gitignore`:

```gitignore
*.sock
*.state.json
__pycache__/
*.pyc
```

## Moving The Project

The script stores `config`, `state`, and the control socket next to itself. If you move `hypr-restore-windows.py`, those files will be read from and written to the new directory.

However, `hypr-window-memory.service` contains a concrete `ExecStart`. If the project does not live in `~/.config/systemd/user`, update `ExecStart`, then run:

```bash
systemctl --user daemon-reload
systemctl --user restart hypr-window-memory.service
```

## Troubleshooting

Logs:

```bash
journalctl --user -u hypr-window-memory.service -f
```

If the CLI says the control socket is unavailable, the service is not running or an older version is running:

```bash
systemctl --user status hypr-window-memory.service --no-pager
systemctl --user restart hypr-window-memory.service
```
