#!/usr/bin/env python3
"""Local tests for Hatch Bridge v1.1.0 inbox, with a stubbed Home Assistant."""
import json
import os
import sys
import tempfile

# --- test env before importing the app -------------------------------------
tmp = tempfile.mkdtemp(prefix="bridgetest-")
opts_path = os.path.join(tmp, "options.json")
with open(opts_path, "w") as f:
    json.dump(
        {
            "api_token": "test-token-123",
            "log_level": "warning",
            "prompt_entity_id": "input_text.muse_prompt",
            "response_entity_id": "input_text.muse_response",
            "inbox_poll_secs": 3,
        },
        f,
    )
os.environ["HATCH_BRIDGE_OPTIONS"] = opts_path
os.environ["SUPERVISOR_TOKEN"] = "dummy-supervisor-token"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hatch_bridge"))
import app as bridge  # noqa: E402

AUTH = {"Authorization": "Bearer test-token-123"}


class FakeHA:
    """Stub for bridge.ha_api."""

    def __init__(self):
        self.states = {}  # entity_id -> state string
        self.notifications = []  # persistent_notification.create payloads
        self.set_values = []  # input_text.set_value calls

    def __call__(self, method, path, json_body=None):
        if method == "GET" and path.startswith("/states/"):
            eid = path[len("/states/") :]
            if eid in self.states:
                return {"entity_id": eid, "state": self.states[eid]}, 200, None
            return {"message": "not found"}, 404, None
        if method == "POST" and path == "/services/input_text/set_value":
            self.set_values.append(json_body)
            self.states[json_body["entity_id"]] = json_body["value"]
            return {}, 200, None
        if method == "POST" and path == "/services/persistent_notification/create":
            self.notifications.append(json_body)
            return {}, 200, None
        raise AssertionError("unexpected HA call: %s %s" % (method, path))


fake = FakeHA()
bridge.ha_api = fake
client = bridge.app.test_client()

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("PASS %s" % name)
    else:
        failed += 1
        print("FAIL %s %s" % (name, detail))


# --- auth behaviour ---------------------------------------------------------
r = client.get("/api/inbox/status")
check("status is public", r.status_code == 200, r.status_code)
body = r.get_json()
check("status empty inbox", body["pending"] == 0 and body["latest_id"] is None, body)

r = client.get("/api/inbox")
check("inbox without auth -> 401", r.status_code == 401, r.status_code)
r = client.get("/api/inbox", headers={"Authorization": "Bearer wrong"})
check("inbox wrong token -> 401", r.status_code == 401, r.status_code)
r = client.get("/api/inbox", headers=AUTH)
check("inbox with token -> 200", r.status_code == 200, r.status_code)

# --- watcher: new text is queued and helper cleared --------------------------
fake.states["input_text.muse_prompt"] = "turn on the kitchen light"
last_seen, found = bridge.poll_prompt_helper(None)
check("helper found", found is True)
check("prompt queued", len(bridge.inbox_pending) == 1, len(bridge.inbox_pending))
p = bridge.inbox_pending[0]
check("prompt fields", p["text"] == "turn on the kitchen light" and len(p["id"]) == 12, p)
check("helper cleared", fake.states["input_text.muse_prompt"] == "")
check("last_seen set", last_seen == "turn on the kitchen light", last_seen)

# poll again while clear: dedupe marker resets
last_seen, found = bridge.poll_prompt_helper(last_seen)
check("clear resets dedupe marker", last_seen is None, last_seen)
check("no duplicate queued", len(bridge.inbox_pending) == 1)

# identical text typed again must still queue (regression test for dedupe bug)
fake.states["input_text.muse_prompt"] = "turn on the kitchen light"
last_seen, found = bridge.poll_prompt_helper(last_seen)
check("identical retype queued", len(bridge.inbox_pending) == 2, len(bridge.inbox_pending))

# --- list + status -----------------------------------------------------------
r = client.get("/api/inbox", headers=AUTH)
prompts = r.get_json()["prompts"]
check("inbox lists 2 prompts", len(prompts) == 2, prompts)
r = client.get("/api/inbox/status")
st = r.get_json()
check("status pending=2 + latest_id", st["pending"] == 2 and st["latest_id"] == prompts[-1]["id"], st)

# --- ack flow -----------------------------------------------------------------
r = client.post("/api/inbox/ack", headers=AUTH, json={})
check("ack missing id -> 400", r.status_code == 400, r.status_code)
r = client.post("/api/inbox/ack", headers=AUTH, json={"id": "nope", "response": "x"})
check("ack unknown id -> 404", r.status_code == 404, r.status_code)

pid = prompts[0]["id"]
r = client.post("/api/inbox/ack", headers=AUTH,
                json={"id": pid, "response": "Kitchen light is on."})
check("ack ok", r.status_code == 200 and r.get_json()["remaining"] == 1, r.get_json())
check("prompt removed", all(q["id"] != pid for q in bridge.inbox_pending))
check("HA notified", len(fake.notifications) == 1
      and fake.notifications[0]["notification_id"] == "muse_inbox"
      and fake.notifications[0]["title"] == "Muse"
      and fake.notifications[0]["message"] == "Kitchen light is on.",
      fake.notifications)
# response helper does not exist -> no mirror attempt beyond the GET
check("no response-helper mirror when missing",
      not [c for c in fake.set_values if c["entity_id"] == "input_text.muse_response"])

# with the response helper present, the reply is mirrored
fake.states["input_text.muse_response"] = ""
pid2 = client.get("/api/inbox", headers=AUTH).get_json()["prompts"][0]["id"]
client.post("/api/inbox/ack", headers=AUTH, json={"id": pid2, "response": "Done."})
check("response mirrored to helper",
      fake.states["input_text.muse_response"] == "Done.", fake.states)
r = client.get("/api/inbox/status")
check("inbox drained", r.get_json()["pending"] == 0, r.get_json())

# --- missing helper -----------------------------------------------------------
del fake.states["input_text.muse_prompt"]
last_seen, found = bridge.poll_prompt_helper(None)
check("missing helper -> found=False, no crash", found is False, (last_seen, found))

# --- queue cap -----------------------------------------------------------------
bridge.inbox_pending.clear()
for i in range(55):
    bridge.enqueue_prompt("msg %d" % i, source="test")
check("queue capped at 50", len(bridge.inbox_pending) == 50, len(bridge.inbox_pending))
check("oldest dropped first", bridge.inbox_pending[0]["text"] == "msg 5",
      bridge.inbox_pending[0]["text"])

# --- ensure_prompt_helper websocket handshake ----------------------------------
# Regression test: HA sends {"type": "auth_required"} as the first websocket
# message. The old code treated that first message as the auth reply and
# always failed with "HA websocket auth failed".
import types


class FakeWS:
    """Replicates the real HA websocket handshake: auth_required first."""

    def __init__(self, script):
        self._script = list(script)
        self.sent = []

    def send(self, msg):
        self.sent.append(json.loads(msg))

    def recv(self):
        return json.dumps(self._script.pop(0))

    def close(self):
        pass


created = {}


def fake_create_connection_ok(url, timeout=None):
    ws = FakeWS(
        [
            {"type": "auth_required", "ha_version": "2026.9.1"},
            {"type": "auth_ok", "ha_version": "2026.9.1"},
            {"id": 1, "type": "result", "success": True, "result": None},
        ]
    )
    created["ws"] = ws
    created["url"] = url
    return ws


fake_websocket_module = types.ModuleType("websocket")
fake_websocket_module.create_connection = fake_create_connection_ok
sys.modules["websocket"] = fake_websocket_module

check("helper auto-create succeeds on real handshake",
      bridge.ensure_prompt_helper() is True)
ws = created["ws"]
check("ws connected to supervisor websocket",
      created["url"] == "ws://supervisor/core/websocket", created["url"])
check("auth sent with supervisor token",
      ws.sent[0] == {"type": "auth", "access_token": "dummy-supervisor-token"},
      ws.sent[0])
check("input_text/create sent",
      ws.sent[1] == {"id": 1, "type": "input_text/create", "name": "Muse prompt",
                     "min": 0, "max": 255, "mode": "text"},
      ws.sent[1])


def fake_create_connection_bad(url, timeout=None):
    return FakeWS(
        [
            {"type": "auth_required", "ha_version": "2026.9.1"},
            {"type": "auth_invalid", "message": "bad token"},
        ]
    )


fake_websocket_module.create_connection = fake_create_connection_bad
check("helper auto-create fails cleanly on auth_invalid",
      bridge.ensure_prompt_helper() is False)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
