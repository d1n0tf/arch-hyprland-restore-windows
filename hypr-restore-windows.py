#!/usr/bin/env python3
import asyncio
import copy
import json
import logging
import os
import signal
import time
from pathlib import Path


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
)


STATE_DIR = Path.home() / ".local/share/hypr-window-memory"
STATE_FILE = STATE_DIR / "state.json"

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

IGNORE_CLASSES = {
    "waybar",
    "mako",
    "wofi",
    "hyprpicker",
    "polkit-gnome-authentication-agent-1",
    "xdg-desktop-portal-hyprland",
    "nm-applet",
}

running = True
state = {}
pending_state_sync_task = None
state_sync_lock = None
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
    async with state_sync_lock:
        changed = False
        now = time.time()
        changed_keys = []
        grouped = {}

        for client in await clients():
            key = class_key(client)
            if not key or key in IGNORE_CLASSES:
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
                if not key or key in IGNORE_CLASSES:
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


async def main():
    global state_sync_lock
    load_state()
    state_sync_lock = asyncio.Lock()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_sig)
        except NotImplementedError:
            pass

    await persist_visible_state("startup")

    try:
        await asyncio.gather(event_loop(), fallback_poll_loop())
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
    asyncio.run(main())
