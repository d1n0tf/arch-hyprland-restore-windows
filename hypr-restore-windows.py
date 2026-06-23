#!/usr/bin/env python3
import asyncio
import argparse
import copy
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
)


STATE_DIR = Path(__file__).resolve().parent
STATE_FILE = STATE_DIR / "hypr-restore-windows.state.json"
CONFIG_FILE = STATE_DIR / "hypr-restore-windows.config.json"
CONTROL_SOCKET_ENV = "HYPR_RESTORE_CONTROL_SOCKET"

RESTORE_DELAY = 0.12
RESTORE_RETRIES = 20
RESTORE_RETRY_DELAY = 0.05
STATE_SYNC_DELAY = 0.20
POST_RESTORE_STATE_SYNC_DELAY = 0.15
SOCKET_READ_TIMEOUT = 0.50
FALLBACK_POLL_INTERVAL = 0.75
WORKSPACE_SETTLE_DELAY = 0.02
FLOAT_SETTLE_DELAY = 0.04
PRE_GEOM_SETTLE_DELAY = 0.03

# Toggle per-window animation suppression during restore.
RESTORE_DISABLE_ANIMATIONS = False

# Only move floating windows during restore if their saved size is at least this large.
RESTORE_MOVE_MIN_WIDTH = 1600
RESTORE_MOVE_MIN_HEIGHT = 800

# Heuristics for ignoring transient helper windows, such as Electron tooltips/popovers.
AUX_WINDOW_MAX_WIDTH_RATIO = 0.55
AUX_WINDOW_MAX_HEIGHT_RATIO = 0.55
AUX_WINDOW_MAX_AREA_RATIO = 0.35

STATE_EVENTS = {
    "closewindow",
    "movewindow",
    "movewindowv2",
    "changefloatingmode",
    "fullscreen",
    "moveworkspace",
    "moveworkspacev2",
}

DEFAULT_IGNORE_CLASSES = {
    "waybar",
    "mako",
    "wofi",
    "hyprpicker",
    "polkit-gnome-authentication-agent-1",
    "xdg-desktop-portal-hyprland",
    "nm-applet",
    "org.gnome.gThumb",
    "steam",
}

running = True
state = {}
dynamic_ignore_classes = set()
pending_state_sync_task = None
state_sync_lock = None
control_lock = None
restoring_keys = set()


def load_state():
    global state
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            state = {}
    else:
        state = {}


def save_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


def normalize_class_names(class_names):
    return {str(name).strip() for name in class_names if str(name).strip()}


def load_config():
    global dynamic_ignore_classes

    if not CONFIG_FILE.exists():
        dynamic_ignore_classes = set()
        return

    data = json.loads(CONFIG_FILE.read_text())
    if isinstance(data, list):
        class_names = data
    elif isinstance(data, dict):
        class_names = data.get("ignore_classes", [])
    else:
        class_names = []

    dynamic_ignore_classes = normalize_class_names(class_names)


def save_config():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {"ignore_classes": sorted(dynamic_ignore_classes)}
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    tmp.replace(CONFIG_FILE)


def all_ignore_classes():
    return DEFAULT_IGNORE_CLASSES | dynamic_ignore_classes


def is_ignored_class(class_name):
    return class_name in all_ignore_classes()


def control_socket_path():
    override = os.environ.get(CONTROL_SOCKET_ENV)
    if override:
        return Path(override).expanduser()
    return STATE_DIR / "hypr-restore-windows.control.sock"


def normalize_addr(addr):
    addr = str(addr).strip()
    if not addr:
        return ""
    if addr.startswith("0x"):
        return addr
    return f"0x{addr}"


def window_selector(addr):
    return f"address:{normalize_addr(addr)}"


def lua_quote(value):
    return json.dumps(value, ensure_ascii=False)


def class_key(client):
    return (client.get("initialClass") or client.get("class") or "").strip()


def extract_geom(client):
    at = client.get("at") or [client.get("x"), client.get("y")]
    size = client.get("size") or [client.get("width"), client.get("height")]
    if not at or not size or at[0] is None or size[0] is None:
        return None
    return {
        "x": int(at[0]),
        "y": int(at[1]),
        "w": int(size[0]),
        "h": int(size[1]),
    }


def workspace_ref(client):
    ws = client.get("workspace") or {}
    if not isinstance(ws, dict):
        return None
    ws_id = ws.get("id")
    ws_name = ws.get("name")
    if ws_name and str(ws_name) != str(ws_id):
        return f"name:{ws_name}"
    if ws_id is not None:
        return str(ws_id)
    return None


def build_state_entry(client, now):
    entry = {
        "floating": bool(client.get("floating", False)),
        "updated": now,
        "workspace": workspace_ref(client),
    }
    if entry["floating"]:
        geom = extract_geom(client)
        if geom:
            entry["geom"] = geom
    return entry


def same_state_entry(old_entry, new_entry):
    if not isinstance(old_entry, dict):
        return False
    return (
        old_entry.get("floating") == new_entry.get("floating")
        and old_entry.get("workspace") == new_entry.get("workspace")
        and old_entry.get("geom") == new_entry.get("geom")
    )


def window_matches_state(client, saved_state):
    if client is None:
        return False

    want_floating = bool(saved_state.get("floating", False))
    if bool(client.get("floating", False)) != want_floating:
        return False

    target_ws = saved_state.get("workspace")
    if target_ws and workspace_ref(client) != target_ws:
        return False

    if want_floating:
        geom = saved_state.get("geom")
        if not geom:
            return True
        current_geom = extract_geom(client)
        if not current_geom:
            return False
        if not should_move_window(saved_state):
            return (
                current_geom.get("w") == geom.get("w")
                and current_geom.get("h") == geom.get("h")
            )
        return current_geom == geom

    return True


def window_area(client):
    geom = extract_geom(client)
    if not geom:
        return 0
    return geom["w"] * geom["h"]


def looks_like_aux_window(client, reference_state):
    ref_geom = (reference_state or {}).get("geom") or {}
    current_geom = extract_geom(client)
    if not ref_geom or not current_geom:
        return False

    width_ratio = current_geom["w"] / max(ref_geom.get("w", 1), 1)
    height_ratio = current_geom["h"] / max(ref_geom.get("h", 1), 1)
    area_ratio = (
        (current_geom["w"] * current_geom["h"])
        / max(ref_geom.get("w", 1) * ref_geom.get("h", 1), 1)
    )
    return (
        width_ratio <= AUX_WINDOW_MAX_WIDTH_RATIO
        and height_ratio <= AUX_WINDOW_MAX_HEIGHT_RATIO
        and area_ratio <= AUX_WINDOW_MAX_AREA_RATIO
    )


def choose_primary_client(clients_for_class):
    def client_score(client):
        return (
            client.get("mapped", True),
            client.get("visible", True),
            client.get("acceptsInput", True),
            not client.get("hidden", False),
            window_area(client),
        )

    return max(clients_for_class, key=client_score, default=None)


async def hyprctl(*args):
    proc = await asyncio.create_subprocess_exec(
        "hyprctl",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    stdout = out.decode()
    stderr = err.decode()
    message = (stderr or stdout).strip()
    if proc.returncode != 0:
        raise RuntimeError(message)
    if stdout.lstrip().startswith("error:"):
        raise RuntimeError(stdout.strip())
    return stdout


async def clients():
    raw = await hyprctl("clients", "-j")
    return json.loads(raw)


def find_client_by_address(clients_list, addr):
    addr = normalize_addr(addr).lower().replace("0x", "")
    for client in clients_list:
        client_addr = str(client.get("address", "")).lower().replace("0x", "")
        if client_addr == addr:
            return client
    return None


async def dispatch(expr):
    await hyprctl("dispatch", expr)


async def dispatch_batch(*expressions):
    batch = " ; ".join(f"dispatch {expr}" for expr in expressions if expr)
    if batch:
        await hyprctl("--batch", batch)


async def set_window_prop(window_addr, prop, value):
    selector = lua_quote(window_selector(window_addr))
    await dispatch(
        f"hl.dsp.window.set_prop({{ prop = {lua_quote(prop)}, value = {lua_quote(str(value))}, window = {selector} }})"
    )


async def move_to_workspace(window_addr, workspace):
    selector = lua_quote(window_selector(window_addr))
    workspace = lua_quote(str(workspace))
    await dispatch(
        f"hl.dsp.window.move({{ workspace = {workspace}, follow = false, window = {selector} }})"
    )


async def set_floating(window_addr, enabled):
    selector = lua_quote(window_selector(window_addr))
    action = "set" if enabled else "unset"
    await dispatch(
        f"hl.dsp.window.float({{ action = {lua_quote(action)}, window = {selector} }})"
    )


def resize_window_expr(window_addr, width, height):
    selector = lua_quote(window_selector(window_addr))
    return (
        f"hl.dsp.window.resize({{ x = {int(width)}, y = {int(height)}, relative = false, window = {selector} }})"
    )


def move_window_expr(window_addr, x, y):
    selector = lua_quote(window_selector(window_addr))
    return (
        f"hl.dsp.window.move({{ x = {int(x)}, y = {int(y)}, relative = false, window = {selector} }})"
    )


def should_move_window(saved_state):
    geom = saved_state.get("geom") or {}
    return (
        geom.get("w", 0) >= RESTORE_MOVE_MIN_WIDTH
        and geom.get("h", 0) >= RESTORE_MOVE_MIN_HEIGHT
    )


async def persist_visible_state(reason):
    async with state_sync_lock:  # type: ignore
        changed = False
        now = time.time()
        changed_keys = []
        grouped = {}

        for client in await clients():
            key = class_key(client)
            if not key or is_ignored_class(key):
                continue
            if key in restoring_keys:
                continue
            grouped.setdefault(key, []).append(client)

        for key, clients_for_class in grouped.items():
            primary_client = choose_primary_client(clients_for_class)
            if not primary_client:
                continue
            if looks_like_aux_window(primary_client, state.get(key)):
                continue

            entry = build_state_entry(primary_client, now)
            if same_state_entry(state.get(key), entry):
                continue

            state[key] = entry
            changed = True
            changed_keys.append(key)

        if changed:
            save_state()
            logging.info("STATE SAVED reason=%s keys=%s", reason, ",".join(changed_keys))


async def delayed_state_sync(reason, delay):
    try:
        await asyncio.sleep(delay)
        await persist_visible_state(reason)
    except asyncio.CancelledError:
        pass
    except Exception:
        logging.exception("state sync failed")


def request_state_sync(reason, delay=STATE_SYNC_DELAY):
    global pending_state_sync_task

    if pending_state_sync_task and not pending_state_sync_task.done():
        pending_state_sync_task.cancel()

    pending_state_sync_task = asyncio.create_task(delayed_state_sync(reason, delay))


def state_rows():
    rows = []
    for key, entry in sorted(state.items()):
        geom = entry.get("geom") or {}
        rows.append(
            {
                "class": key,
                "floating": bool(entry.get("floating", False)),
                "workspace": entry.get("workspace") or "",
                "geom": (
                    f'{geom.get("w")}x{geom.get("h")}+{geom.get("x")}+{geom.get("y")}'
                    if geom
                    else ""
                ),
                "updated": entry.get("updated"),
            }
        )
    return rows


def client_rows(clients_list):
    rows = []
    for client in clients_list:
        key = class_key(client)
        geom = extract_geom(client) or {}
        rows.append(
            {
                "address": normalize_addr(client.get("address", "")),
                "class": key,
                "title": client.get("initialTitle") or client.get("title") or "",
                "workspace": workspace_ref(client) or "",
                "floating": bool(client.get("floating", False)),
                "geom": (
                    f'{geom.get("w")}x{geom.get("h")}+{geom.get("x")}+{geom.get("y")}'
                    if geom
                    else ""
                ),
                "ignored": bool(key and is_ignored_class(key)),
            }
        )
    return rows


async def handle_exclude_command(action, class_names):
    global dynamic_ignore_classes

    names = normalize_class_names(class_names)

    if action in {"list", "ls"}:
        return {
            "ok": True,
            "data": {
                "default": sorted(DEFAULT_IGNORE_CLASSES),
                "dynamic": sorted(dynamic_ignore_classes),
                "all": sorted(all_ignore_classes()),
            },
        }

    if action == "add":
        if not names:
            return {"ok": False, "message": "no classes provided"}

        already_default = sorted(names & DEFAULT_IGNORE_CLASSES)
        added = sorted(names - DEFAULT_IGNORE_CLASSES - dynamic_ignore_classes)
        if added:
            dynamic_ignore_classes.update(added)
            save_config()
            request_state_sync("control-exclude-add")

        parts = []
        if added:
            parts.append("added: " + ", ".join(added))
        if already_default:
            parts.append("already built-in: " + ", ".join(already_default))
        if not parts:
            parts.append("no changes")
        return {
            "ok": True,
            "message": "; ".join(parts),
            "data": {"dynamic": sorted(dynamic_ignore_classes)},
        }

    if action in {"remove", "rm", "delete", "del"}:
        if not names:
            return {"ok": False, "message": "no classes provided"}

        removed = sorted(names & dynamic_ignore_classes)
        built_in = sorted(names & DEFAULT_IGNORE_CLASSES)
        missing = sorted(names - dynamic_ignore_classes - DEFAULT_IGNORE_CLASSES)
        if removed:
            dynamic_ignore_classes.difference_update(removed)
            save_config()
            request_state_sync("control-exclude-remove")

        parts = []
        if removed:
            parts.append("removed: " + ", ".join(removed))
        if built_in:
            parts.append("built-in exclusions cannot be removed: " + ", ".join(built_in))
        if missing:
            parts.append("not found: " + ", ".join(missing))
        if not parts:
            parts.append("no changes")
        return {
            "ok": True,
            "message": "; ".join(parts),
            "data": {"dynamic": sorted(dynamic_ignore_classes)},
        }

    if action == "clear":
        if not dynamic_ignore_classes:
            return {"ok": True, "message": "dynamic exclusions already empty"}
        removed = sorted(dynamic_ignore_classes)
        dynamic_ignore_classes = set()
        save_config()
        request_state_sync("control-exclude-clear")
        return {
            "ok": True,
            "message": "removed dynamic exclusions: " + ", ".join(removed),
            "data": {"dynamic": []},
        }

    return {"ok": False, "message": f"unknown exclude action: {action}"}


async def handle_state_command(action, request):
    names = normalize_class_names(request.get("classes", []))

    if action in {"list", "ls"}:
        return {"ok": True, "data": {"state": state_rows()}}

    if action == "show":
        if not names:
            return {"ok": False, "message": "no classes provided"}
        selected = {name: state.get(name) for name in sorted(names) if name in state}
        missing = sorted(names - set(selected))
        return {
            "ok": True,
            "message": ("not found: " + ", ".join(missing)) if missing else "",
            "data": {"state": selected, "missing": missing},
        }

    if action in {"forget", "remove", "rm", "delete", "del"}:
        if not names:
            return {"ok": False, "message": "no classes provided"}

        async with state_sync_lock:  # type: ignore
            removed = sorted(name for name in names if name in state)
            missing = sorted(names - set(removed))
            for name in removed:
                state.pop(name, None)
            if removed:
                save_state()

        parts = []
        if removed:
            parts.append("forgot: " + ", ".join(removed))
        if missing:
            parts.append("not found: " + ", ".join(missing))
        return {
            "ok": True,
            "message": "; ".join(parts) if parts else "no changes",
            "data": {"removed": removed, "missing": missing},
        }

    if action == "clear":
        if not request.get("force"):
            return {"ok": False, "message": "state clear requires --force"}

        async with state_sync_lock:  # type: ignore
            count = len(state)
            state.clear()
            save_state()
        return {"ok": True, "message": f"cleared saved state entries: {count}"}

    return {"ok": False, "message": f"unknown state action: {action}"}


async def handle_control_request(request):
    command = str(request.get("command", "")).strip()

    if command == "status":
        return {
            "ok": True,
            "data": {
                "config_file": str(CONFIG_FILE),
                "control_socket": str(control_socket_path()),
                "default_ignore_count": len(DEFAULT_IGNORE_CLASSES),
                "dynamic_ignore_count": len(dynamic_ignore_classes),
                "ignored_total": len(all_ignore_classes()),
                "pending_state_sync": bool(
                    pending_state_sync_task and not pending_state_sync_task.done()
                ),
                "restoring": sorted(restoring_keys),
                "saved_classes": len(state),
                "state_file": str(STATE_FILE),
            },
        }

    if command == "reload":
        try:
            load_config()
        except Exception as exc:
            logging.exception("config reload failed")
            return {"ok": False, "message": f"config reload failed: {exc}"}
        request_state_sync("control-reload")
        return {
            "ok": True,
            "message": f"config reloaded, dynamic exclusions: {len(dynamic_ignore_classes)}",
        }

    if command == "sync":
        await persist_visible_state("control-sync")
        return {"ok": True, "message": "visible window state synced"}

    if command == "clients":
        return {"ok": True, "data": {"clients": client_rows(await clients())}}

    if command.startswith("exclude."):
        return await handle_exclude_command(
            command.split(".", 1)[1],
            request.get("classes", []),
        )

    if command.startswith("state."):
        return await handle_state_command(command.split(".", 1)[1], request)

    return {"ok": False, "message": f"unknown command: {command}"}


async def handle_control_client(reader, writer):
    response = None
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line:
            return

        try:
            request = json.loads(line.decode())
        except Exception as exc:
            response = {"ok": False, "message": f"invalid request: {exc}"}
        else:
            async with control_lock:  # type: ignore
                response = await handle_control_request(request)
    except Exception as exc:
        logging.exception("control request failed")
        response = {"ok": False, "message": str(exc)}
    finally:
        if response is not None:
            writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
            try:
                await writer.drain()
            except Exception:
                pass
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def control_server_loop():
    sock = control_socket_path()
    sock.parent.mkdir(parents=True, exist_ok=True)
    if sock.exists():
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock))
        except OSError:
            sock.unlink()
        else:
            writer.close()
            await writer.wait_closed()
            raise RuntimeError(f"control socket already in use: {sock}")

    server = await asyncio.start_unix_server(handle_control_client, path=str(sock))
    os.chmod(sock, 0o600)
    logging.info("CONTROL SOCKET %s", sock)

    try:
        async with server:
            while running:
                await asyncio.sleep(0.25)
    finally:
        server.close()
        await server.wait_closed()
        try:
            if sock.exists():
                sock.unlink()
        except Exception:
            logging.exception("failed to remove control socket")


async def apply_saved_state(addr, current_client, saved_state):
    target_ws = saved_state.get("workspace")
    if target_ws and workspace_ref(current_client) != target_ws:
        await move_to_workspace(addr, target_ws)
        await asyncio.sleep(WORKSPACE_SETTLE_DELAY)

    want_floating = bool(saved_state.get("floating", False))
    is_floating = bool(current_client.get("floating", False))

    if want_floating and not is_floating:
        await set_floating(addr, True)
        await asyncio.sleep(FLOAT_SETTLE_DELAY)
    elif not want_floating and is_floating:
        await set_floating(addr, False)
        await asyncio.sleep(FLOAT_SETTLE_DELAY)
        return

    if not want_floating:
        return

    geom = saved_state.get("geom")
    if not geom:
        return

    expressions = [resize_window_expr(addr, geom["w"], geom["h"])]
    if should_move_window(saved_state):
        expressions.append(move_window_expr(addr, geom["x"], geom["y"]))

    await asyncio.sleep(PRE_GEOM_SETTLE_DELAY)
    await dispatch_batch(*expressions)


async def restore_window(addr):
    addr = normalize_addr(addr)
    if not addr:
        return

    await asyncio.sleep(RESTORE_DELAY)
    logging.info("RESTORE START %s", addr)

    restore_key = None
    saved_state = None
    no_anim_enabled = False

    try:
        for attempt in range(1, RESTORE_RETRIES + 1):
            try:
                clients_list = await clients()
                client = find_client_by_address(clients_list, addr)
                if not client:
                    await asyncio.sleep(RESTORE_RETRY_DELAY)
                    continue

                key = class_key(client)
                if not key or is_ignored_class(key):
                    return

                if saved_state is None:
                    current_state = state.get(key)
                    if not current_state:
                        logging.info("RESTORE SKIP %s class=%s no saved state", addr, key)
                        return
                    saved_state = copy.deepcopy(current_state)
                    if looks_like_aux_window(client, saved_state):
                        logging.info("RESTORE SKIP %s class=%s helper-like window", addr, key)
                        return
                    restore_key = key
                    restoring_keys.add(key)
                    if RESTORE_DISABLE_ANIMATIONS:
                        await set_window_prop(addr, "no_anim", "1")
                        no_anim_enabled = True

                logging.info(
                    "RESTORE TRY %s attempt=%s class=%s floating=%s at=%s size=%s target=%s",
                    addr,
                    attempt,
                    key,
                    client.get("floating"),
                    client.get("at"),
                    client.get("size"),
                    saved_state,
                )

                await apply_saved_state(addr, client, saved_state)
                await asyncio.sleep(RESTORE_RETRY_DELAY)

                refreshed_client = find_client_by_address(await clients(), addr)
                if window_matches_state(refreshed_client, saved_state):
                    logging.info("RESTORE OK %s class=%s", addr, key)
                    request_state_sync("restore", POST_RESTORE_STATE_SYNC_DELAY)
                    return
            except Exception:
                logging.exception("restore failed")
                await asyncio.sleep(RESTORE_RETRY_DELAY)

        logging.warning("RESTORE GAVE UP %s", addr)
    finally:
        if no_anim_enabled:
            try:
                await set_window_prop(addr, "no_anim", "unset")
            except Exception:
                logging.exception("failed to unset no_anim for %s", addr)
        if restore_key:
            restoring_keys.discard(restore_key)


async def event_loop():
    rt = os.environ.get("XDG_RUNTIME_DIR")
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not rt or not sig:
        raise RuntimeError("XDG_RUNTIME_DIR or HYPRLAND_INSTANCE_SIGNATURE is missing")

    sock = Path(rt) / "hypr" / sig / ".socket2.sock"
    reader, writer = await asyncio.open_unix_connection(str(sock))

    try:
        while running:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=SOCKET_READ_TIMEOUT)
            except asyncio.TimeoutError:
                continue

            if not line:
                await asyncio.sleep(0.1)
                continue

            text = line.decode(errors="ignore").strip()
            if ">>" not in text:
                continue

            event, payload = text.split(">>", 1)

            if event == "openwindow":
                addr = payload.split(",", 1)[0].strip()
                logging.info("OPENWINDOW %s", addr)
                if addr:
                    asyncio.create_task(restore_window(addr))
                continue

            if event in STATE_EVENTS:
                request_state_sync(event)
    finally:
        writer.close()
        await writer.wait_closed()


async def fallback_poll_loop():
    while running:
        try:
            await persist_visible_state("poll")
        except Exception:
            logging.exception("poll sync failed")
        await asyncio.sleep(FALLBACK_POLL_INTERVAL)


def handle_sig(*_):
    global running
    running = False


def handle_sighup(*_):
    try:
        load_config()
        request_state_sync("sighup-reload")
        logging.info("CONFIG RELOADED dynamic_exclusions=%s", len(dynamic_ignore_classes))
    except Exception:
        logging.exception("config reload failed")


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Restore Hyprland windows and control the running service."
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("daemon", help="run the long-lived restore service")
    sub.add_parser("status", help="show live service status")
    sub.add_parser("sync", help="persist currently visible window state now")
    sub.add_parser("reload", help="reload config.json in the running service")
    sub.add_parser("clients", help="list currently visible Hyprland clients")

    exclude = sub.add_parser(
        "exclude",
        aliases=["ignore"],
        help="manage live class exclusions",
    )
    exclude_sub = exclude.add_subparsers(dest="action", required=True)
    exclude_sub.add_parser("list", aliases=["ls"], help="list exclusions")
    exclude_add = exclude_sub.add_parser("add", help="add class exclusions")
    exclude_add.add_argument("classes", nargs="+")
    exclude_remove = exclude_sub.add_parser(
        "remove",
        aliases=["rm", "delete", "del"],
        help="remove dynamic class exclusions",
    )
    exclude_remove.add_argument("classes", nargs="+")
    exclude_sub.add_parser("clear", help="remove all dynamic exclusions")

    state_parser = sub.add_parser("state", help="inspect or edit saved restore state")
    state_sub = state_parser.add_subparsers(dest="action", required=True)
    state_sub.add_parser("list", aliases=["ls"], help="list saved state entries")
    state_show = state_sub.add_parser("show", help="show saved state for classes")
    state_show.add_argument("classes", nargs="+")
    state_forget = state_sub.add_parser(
        "forget",
        aliases=["remove", "rm", "delete", "del"],
        help="delete saved state for classes",
    )
    state_forget.add_argument("classes", nargs="+")
    state_clear = state_sub.add_parser("clear", help="delete all saved state")
    state_clear.add_argument("--force", action="store_true")

    return parser


def canonical_action(action):
    aliases = {
        "ls": "list",
        "rm": "remove",
        "delete": "remove",
        "del": "remove",
    }
    return aliases.get(action, action)


def request_from_args(args):
    if args.command in {"exclude", "ignore"}:
        return {
            "command": f"exclude.{canonical_action(args.action)}",
            "classes": getattr(args, "classes", []),
        }

    if args.command == "state":
        return {
            "command": f"state.{canonical_action(args.action)}",
            "classes": getattr(args, "classes", []),
            "force": getattr(args, "force", False),
        }

    return {"command": args.command}


async def send_control_request(request):
    sock = control_socket_path()
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        raise RuntimeError(f"control socket is unavailable: {sock} ({exc})") from exc

    writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    writer.close()
    await writer.wait_closed()

    if not line:
        raise RuntimeError("control socket closed without a response")
    return json.loads(line.decode())


def format_timestamp(value):
    if value is None:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    except Exception:
        return str(value)


def print_table(rows, columns):
    if not rows:
        print("(empty)")
        return

    rendered = []
    for row in rows:
        rendered.append([str(row.get(key, "")) for key, _ in columns])

    widths = []
    for index, (_, header) in enumerate(columns):
        widths.append(max(len(header), *(len(row[index]) for row in rendered)))

    print("  ".join(header.ljust(widths[index]) for index, (_, header) in enumerate(columns)))
    print("  ".join("-" * width for width in widths))
    for row in rendered:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def print_cli_response(request, response):
    command = request.get("command")
    data = response.get("data") or {}
    message = response.get("message")

    if command == "status":
        status = data
        for key in (
            "control_socket",
            "config_file",
            "state_file",
            "saved_classes",
            "ignored_total",
            "dynamic_ignore_count",
            "default_ignore_count",
            "pending_state_sync",
        ):
            print(f"{key}: {status.get(key)}")
        restoring = status.get("restoring") or []
        print("restoring: " + (", ".join(restoring) if restoring else "-"))
        return

    if command == "clients":
        rows = data.get("clients", [])
        print_table(
            rows,
            (
                ("ignored", "ignored"),
                ("class", "class"),
                ("workspace", "workspace"),
                ("floating", "floating"),
                ("geom", "geom"),
                ("title", "title"),
                ("address", "address"),
            ),
        )
        return

    if command == "exclude.list":
        dynamic = data.get("dynamic", [])
        default = data.get("default", [])
        print("dynamic exclusions:")
        for name in dynamic:
            print(f"  {name}")
        if not dynamic:
            print("  (none)")
        print("built-in exclusions:")
        for name in default:
            print(f"  {name}")
        return

    if command == "state.list":
        rows = data.get("state", [])
        for row in rows:
            row["updated"] = format_timestamp(row.get("updated"))
        print_table(
            rows,
            (
                ("class", "class"),
                ("workspace", "workspace"),
                ("floating", "floating"),
                ("geom", "geom"),
                ("updated", "updated"),
            ),
        )
        return

    if command == "state.show":
        print(json.dumps(data.get("state", {}), ensure_ascii=False, indent=2, sort_keys=True))
        if message:
            print(message, file=sys.stderr)
        return

    if message:
        print(message)


def run_cli(argv):
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not args.command or args.command == "daemon":
        asyncio.run(main())
        return 0

    request = request_from_args(args)
    try:
        response = asyncio.run(send_control_request(request))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not response.get("ok"):
        print(f"error: {response.get('message', 'request failed')}", file=sys.stderr)
        return 1

    print_cli_response(request, response)
    return 0


async def main():
    global state_sync_lock, control_lock
    load_state()
    try:
        load_config()
    except Exception:
        logging.exception("config load failed")
    state_sync_lock = asyncio.Lock()
    control_lock = asyncio.Lock()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_sig)
        except NotImplementedError:
            pass
    try:
        loop.add_signal_handler(signal.SIGHUP, handle_sighup)
    except (AttributeError, NotImplementedError):
        pass

    await persist_visible_state("startup")

    try:
        await asyncio.gather(event_loop(), fallback_poll_loop(), control_server_loop())
    finally:
        if pending_state_sync_task and not pending_state_sync_task.done():
            pending_state_sync_task.cancel()
            try:
                await pending_state_sync_task
            except asyncio.CancelledError:
                pass

        try:
            await persist_visible_state("shutdown")
        except Exception:
            logging.exception("shutdown state sync failed")


if __name__ == "__main__":
    sys.exit(run_cli(sys.argv[1:]))
