#!/usr/bin/env python3
"""Hatch Bridge: a token-protected HTTP API that lets the Hatch assistant
read Home Assistant states and call Home Assistant services.

Runs as a Home Assistant app. It talks to Home Assistant Core
through the Supervisor API proxy using the automatic SUPERVISOR_TOKEN,
so no long-lived HA token needs to be configured.

Security model:
  * Every /api/* route requires `Authorization: Bearer <api_token>`.
  * The token is set by the user in the app Configuration tab.
  * Only a small allow-list of endpoints is exposed (no generic proxy).
  * TLS termination is expected to happen in front of this service
    (reverse proxy or Cloudflare Tunnel) before it is reachable remotely.
"""

import hmac
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from functools import wraps

import requests
from flask import Flask, Response, jsonify, request

OPTIONS_PATH = os.environ.get("HATCH_BRIDGE_OPTIONS", "/data/options.json")
HA_API = "http://supervisor/core/api"
LISTEN_PORT = 8099
VERSION = "1.3.0"

log = logging.getLogger("hatch-bridge")

# HA domain/service names are lowercase alphanumerics + underscore.
SAFE_NAME = re.compile(r"^[a-z0-9_]+$")


def load_options():
    try:
        with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("Options file %s not found, using defaults", OPTIONS_PATH)
        return {}
    except (json.JSONDecodeError, OSError) as err:
        log.error("Could not read options file: %s", err)
        return {}


OPTIONS = load_options()
API_TOKEN = str(OPTIONS.get("api_token") or "")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

logging.basicConfig(
    level=getattr(logging, str(OPTIONS.get("log_level", "info")).upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)

if not API_TOKEN:
    log.error(
        "Refusing to start: 'api_token' is empty. "
        "Set a long random token in the app Configuration tab and restart."
    )
    sys.exit(1)
if not SUPERVISOR_TOKEN:
    log.error(
        "Refusing to start: SUPERVISOR_TOKEN is not set. "
        "The app needs Supervisor API access (hassio_api)."
    )
    sys.exit(1)

# --- Prompt inbox (v1.1.0; branding made configurable in v1.2.0) --------------
# A text helper in Home Assistant (default input_text.<assistant>_prompt)
# acts as the "prompt me" entity: anything typed into it (by the dashboard
# or by an automation) is queued here, picked up by the assistant's
# watcher, acted on, and answered back via a persistent notification.
ASSISTANT_NAME = str(OPTIONS.get("assistant_name") or "Muse").strip() or "Muse"
ASSISTANT_SLUG = re.sub(r"[^a-z0-9]+", "_", ASSISTANT_NAME.lower()).strip("_") or "assistant"
PROMPT_ENTITY = str(
    OPTIONS.get("prompt_entity_id") or "input_text.%s_prompt" % ASSISTANT_SLUG
).strip()
RESPONSE_ENTITY = str(
    OPTIONS.get("response_entity_id") or "input_text.%s_response" % ASSISTANT_SLUG
).strip()
try:
    INBOX_POLL_SECS = max(1, int(OPTIONS.get("inbox_poll_secs", 3)))
except (TypeError, ValueError):
    INBOX_POLL_SECS = 3

inbox_lock = threading.Lock()
inbox_pending = deque()  # dicts: id, text, received_at, source
inbox_done = deque(maxlen=20)  # recently acked, for debugging

app = Flask(__name__)


def require_auth(view):
    """Reject requests without the correct Bearer token (constant-time compare)."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(token, API_TOKEN):
            return jsonify({"error": "unauthorized"}), 401
        return view(*args, **kwargs)

    return wrapper


def ha_api(method, path, json_body=None):
    """Call the Home Assistant API via the Supervisor proxy.

    Returns (data, status_code, error). error is None on success.
    """
    try:
        resp = requests.request(
            method,
            HA_API + path,
            headers={"Authorization": "Bearer %s" % SUPERVISOR_TOKEN},
            json=json_body,
            timeout=20,
        )
    except requests.RequestException as err:
        log.error("Home Assistant API unreachable: %s", err)
        return None, 502, "home_assistant_unreachable: %s" % err
    try:
        return resp.json(), resp.status_code, None
    except ValueError:
        return {"raw": resp.text}, resp.status_code, None


def enqueue_prompt(text, source):
    """Queue a prompt from Home Assistant. Returns the queued prompt dict."""
    prompt = {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
    }
    with inbox_lock:
        inbox_pending.append(prompt)
        while len(inbox_pending) > 50:  # bound memory; oldest dropped first
            inbox_pending.popleft()
    log.info("Inbox: queued prompt %s from %s: %.80s", prompt["id"], source, text)
    return prompt


def ensure_prompt_helper():
    """Best-effort: create the prompt input_text helper via the HA websocket
    API. Returns True if the helper now exists. Never raises."""
    try:
        from websocket import create_connection
    except ImportError:
        log.warning("websocket-client is not installed; cannot auto-create %s", PROMPT_ENTITY)
        return False
    ws = None
    try:
        ws = create_connection("ws://supervisor/core/websocket", timeout=10)
        # HA sends {"type": "auth_required", ...} as the first message on every
        # new connection; consume it before authenticating.
        json.loads(ws.recv())
        ws.send(json.dumps({"type": "auth", "access_token": SUPERVISOR_TOKEN}))
        hello = json.loads(ws.recv())
        if hello.get("type") != "auth_ok":
            log.warning("HA websocket auth failed; cannot auto-create %s", PROMPT_ENTITY)
            return False
        ws.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "input_text/create",
                    "name": "%s prompt" % ASSISTANT_NAME,
                    "min": 0,
                    "max": 255,
                    "mode": "text",
                }
            )
        )
        resp = json.loads(ws.recv())
        if resp.get("success"):
            log.info("Auto-created %s via the HA websocket API", PROMPT_ENTITY)
            return True
        log.warning("input_text/create failed: %s", resp.get("error"))
        return False
    except Exception as err:
        log.warning("Could not auto-create %s: %s", PROMPT_ENTITY, err)
        return False
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def poll_prompt_helper(last_seen):
    """Single inbox poll: read the prompt helper, queue new text, clear it.

    Returns (last_seen, helper_found). Never raises.
    """
    try:
        data, status, err = ha_api("GET", "/states/%s" % PROMPT_ENTITY)
    except Exception as exc:
        log.warning("Inbox watcher: HA read failed: %s", exc)
        return last_seen, True
    if status == 404:
        return last_seen, False  # helper missing; watcher handles creation
    if err or status != 200:
        log.warning("Inbox watcher: could not read %s (status %s)", PROMPT_ENTITY, status)
        return last_seen, True
    value = str((data or {}).get("state", "") or "").strip()
    if not value or value in ("unknown", "unavailable"):
        return None, True  # helper is clear; reset the dedupe marker
    if value != last_seen:
        enqueue_prompt(value, source=PROMPT_ENTITY)
        last_seen = value
    # Always attempt to clear a non-empty helper so the next prompt registers.
    _, clear_status, clear_err = ha_api(
        "POST",
        "/services/input_text/set_value",
        json_body={"entity_id": PROMPT_ENTITY, "value": ""},
    )
    if clear_err or clear_status != 200:
        log.warning("Inbox watcher: could not clear %s", PROMPT_ENTITY)
    return last_seen, True


def inbox_watcher(stop_event):
    """Background loop: turn new prompt-helper text into inbox prompts."""
    log.info("Inbox watcher started, watching %s every %ds", PROMPT_ENTITY, INBOX_POLL_SECS)
    # Best-effort: make sure the prompt entity exists (retry rarely).
    create_attempted_at = 0.0
    helper_warned = False
    last_seen = None
    while not stop_event.is_set():
        last_seen, found = poll_prompt_helper(last_seen)
        if not found and time.time() - create_attempted_at > 300:
            create_attempted_at = time.time()
            if ensure_prompt_helper():
                helper_warned = False
            elif not helper_warned:
                log.warning(
                    "%s not found. Create an input_text helper named '%s prompt' "
                    "(HA Settings > Devices & services > Helpers > Text).",
                    PROMPT_ENTITY,
                    ASSISTANT_NAME,
                )
                helper_warned = True
        elif found:
            helper_warned = False
        stop_event.wait(INBOX_POLL_SECS)


def notify_ha(response, prompt_id):
    """Report the assistant's answer back into Home Assistant. Never raises."""
    message = (response or "").strip() or "(done)"
    try:
        # 1) Persistent notification: visible in the HA UI and companion app.
        ha_api(
            "POST",
            "/services/persistent_notification/create",
            json_body={
                "notification_id": "%s_inbox" % ASSISTANT_SLUG,
                "title": ASSISTANT_NAME,
                "message": message,
            },
        )
        # 2) Best-effort: mirror into the response helper for dashboards.
        data, status, err = ha_api("GET", "/states/%s" % RESPONSE_ENTITY)
        if not err and status == 200 and data:
            ha_api(
                "POST",
                "/services/input_text/set_value",
                json_body={"entity_id": RESPONSE_ENTITY, "value": message[:255]},
            )
    except Exception as exc:
        log.warning("Inbox: failed to notify HA about %s: %s", prompt_id, exc)


@app.get("/ping")
def ping():
    """Unauthenticated liveness probe for the Supervisor watchdog."""
    return jsonify({"status": "ok", "service": "hatch-bridge", "version": VERSION})


@app.get("/")
def index():
    return Response(INDEX_HTML, content_type="text/html; charset=utf-8")


@app.get("/api/health")
@require_auth
def health():
    data, status, err = ha_api("GET", "/config")
    if err or status != 200:
        return jsonify({"status": "degraded", "homeassistant": "unreachable"}), 503
    return jsonify(
        {
            "status": "ok",
            "homeassistant": "ok",
            "ha_version": data.get("version"),
            "location": data.get("location_name"),
        }
    )


@app.get("/api/states")
@require_auth
def states():
    """All entity states. Optional ?domain=light filter to slim the payload."""
    domain = request.args.get("domain", "")
    if domain and not SAFE_NAME.match(domain):
        return jsonify({"error": "invalid domain"}), 400
    data, status, err = ha_api("GET", "/states")
    if err:
        return jsonify({"error": err}), 502
    if status != 200:
        return jsonify(data), status
    if domain:
        prefix = domain + "."
        data = [s for s in data if str(s.get("entity_id", "")).startswith(prefix)]
    return jsonify(data)


@app.get("/api/states/<entity_id>")
@require_auth
def state(entity_id):
    data, status, err = ha_api("GET", "/states/%s" % entity_id)
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data), status


@app.get("/api/services")
@require_auth
def services():
    """List all available service domains and their services (for discovery)."""
    data, status, err = ha_api("GET", "/services")
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data), status


@app.post("/api/services/<domain>/<service>")
@require_auth
def call_service(domain, service):
    """Call a Home Assistant service. JSON body is forwarded as service data."""
    if not SAFE_NAME.match(domain) or not SAFE_NAME.match(service):
        return jsonify({"error": "invalid domain or service"}), 400
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "body must be a JSON object"}), 400
    log.info("Service call: %s.%s data=%s", domain, service, body)
    data, status, err = ha_api("POST", "/services/%s/%s" % (domain, service), json_body=body)
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data), status


@app.get("/api/config")
@require_auth
def ha_config():
    data, status, err = ha_api("GET", "/config")
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data), status


@app.get("/api/inbox/status")
def inbox_status():
    """Public (no auth): how many prompts are waiting, and the newest id.

    This is the only unauthenticated /api/* route. It reveals no prompt
    content, only a count and an opaque id, so the Hatch watcher's polling
    script can detect new prompts without holding the API token.
    """
    with inbox_lock:
        pending = len(inbox_pending)
        latest_id = inbox_pending[-1]["id"] if inbox_pending else None
    return jsonify({"pending": pending, "latest_id": latest_id, "version": VERSION})


@app.get("/api/inbox")
@require_auth
def inbox_list():
    """List pending prompts (oldest first)."""
    with inbox_lock:
        prompts = list(inbox_pending)
    return jsonify({"prompts": prompts})


@app.post("/api/inbox/ack")
@require_auth
def inbox_ack():
    """Acknowledge a prompt as handled. Body: {"id": "...", "response": "..."}.

    The response is reported back into Home Assistant as a persistent
    notification (and, best-effort, into the response text helper).
    """
    body = request.get_json(silent=True) or {}
    prompt_id = str(body.get("id", "") or "")
    response = str(body.get("response", "") or "")
    if not prompt_id:
        return jsonify({"error": "missing id"}), 400
    with inbox_lock:
        prompt = next((p for p in inbox_pending if p["id"] == prompt_id), None)
        if prompt is None:
            return jsonify({"error": "unknown prompt id"}), 404
        inbox_pending.remove(prompt)
        inbox_done.append({"id": prompt_id, "response": response})
        remaining = len(inbox_pending)
    log.info("Inbox: acked prompt %s (%d remaining)", prompt_id, remaining)
    notify_ha(response, prompt_id)
    return jsonify({"ok": True, "remaining": remaining})


INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Hatch Bridge</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:4rem auto;padding:0 1rem;color:#222}
code{background:#f2f2f2;padding:.15rem .4rem;border-radius:4px}</style>
</head><body>
<h1>Hatch Bridge is running</h1>
<p>This is a private API bridge for your Hatch AI assistant. It is protected
by a bearer token configured in the app <b>Configuration</b> tab.</p>
<ul>
<li><code>GET /ping</code> &ndash; liveness probe (no auth)</li>
<li><code>GET /api/health</code> &ndash; bridge + Home Assistant status</li>
<li><code>GET /api/states?domain=light</code> &ndash; entity states</li>
<li><code>GET /api/states/&lt;entity_id&gt;</code> &ndash; single entity</li>
<li><code>GET /api/services</code> &ndash; available services</li>
<li><code>POST /api/services/&lt;domain&gt;/&lt;service&gt;</code> &ndash; call a service</li>
<li><code>GET /api/config</code> &ndash; Home Assistant configuration info</li>
<li><code>GET /api/inbox</code> &ndash; pending text prompts queued from Home Assistant</li>
<li><code>POST /api/inbox/ack</code> &ndash; mark a prompt handled, with a reply</li>
</ul>
<p><code>GET /api/inbox/status</code> is also public (no auth): it reports only
how many prompts are waiting, so the assistant's watcher can poll it without
the token. It never reveals prompt content.</p>
<p>All other <code>/api/*</code> routes require
<code>Authorization: Bearer &lt;your api_token&gt;</code>.</p>
</body></html>
"""


def main():
    from waitress import serve

    stop_event = threading.Event()
    watcher = threading.Thread(
        target=inbox_watcher, args=(stop_event,), daemon=True, name="inbox-watcher"
    )
    watcher.start()
    log.info("Hatch Bridge v%s listening on port %d", VERSION, LISTEN_PORT)
    try:
        serve(app, host="0.0.0.0", port=LISTEN_PORT)
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
