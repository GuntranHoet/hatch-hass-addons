# Hatch Bridge for Home Assistant

A Home Assistant **app** (add-on) that exposes a small, token-protected HTTP API
so your AI assistant can read entity states and call services on your
Home Assistant instance — from anywhere.

## What it does

- `GET /api/states` — all entity states (optional `?domain=light` filter)
- `GET /api/states/<entity_id>` — one entity
- `GET /api/services` — discover available service domains/services
- `POST /api/services/<domain>/<service>` — call a service (e.g. `light.turn_on`)
- `GET /api/health`, `GET /api/config` — status info
- **Prompt inbox (v1.1.0):** `GET /api/inbox` lists text prompts queued from
  Home Assistant; `POST /api/inbox/ack` marks one handled with a reply.
  `GET /api/inbox/status` is public (no auth) and reports only a pending
  count, so the assistant's watcher can poll it without the token.

Every `/api/*` route except `/api/inbox/status` requires
`Authorization: Bearer <api_token>`.
There is no generic proxy: only these curated endpoints exist.

## Prompt inbox

Type into the prompt `input_text` helper in Home Assistant (the bridge tries
to create it on startup using your configured `assistant_name`, e.g. a Text
helper named "Muse prompt") and your AI assistant picks it up within
~15 seconds, acts on it, and answers back with a persistent notification
titled with the assistant's name. Automations can use the same helper via
`input_text.set_value` to ask the assistant to do something on a trigger —
see `DOCS.md` for an example.

## Upgrading

To move from v1.2.0 to v1.3.0: copy the new `hatch_bridge` folder over the
old one in `/addons/`, then **Settings → Add-ons → Add-on Store → ⋯ →
Check for updates**, open the app and **Rebuild** (or uninstall + reinstall).
Your `api_token` and other options are kept. New in v1.3.0: the default
assistant name is `Muse` instead of `Melnik` — existing installs keep their
configured values, so nothing changes for them.

To move from v1.1.0 to v1.2.0: copy the new `hatch_bridge` folder over the
old one in `/addons/`, then **Settings → Add-ons → Add-on Store → ⋯ →
Check for updates**, open the app and **Rebuild** (or uninstall + reinstall).
Your `api_token` and other options are kept. New in v1.2.0: the
`assistant_name` option (defaults to `Muse`) controls the auto-created
helper's name, the notification title, and the default helper entity IDs —
set it to your own assistant's name.

To move from v1.0.0 to v1.1.0: copy the new `hatch_bridge` folder over the
old one in `/addons/`, then **Settings → Add-ons → Add-on Store → ⋯ →
Check for updates**, open the app and **Rebuild** (or uninstall + reinstall).
Your `api_token` and other options are kept. The app log should show
`Hatch Bridge v1.1.0 listening on port 8099`.

## Requirements

- Home Assistant OS or Home Assistant Supervised (apps need the Supervisor).
  The plain Container/Core installs cannot run apps.

## Install

### Option A — local app (simplest, no GitHub needed)

1. Copy the `hatch_bridge` folder to your Home Assistant machine's `/addons/`
   directory (e.g. via the Samba add-on, or `scp -r hatch_bridge <host>:/addons/`).
2. In Home Assistant go to **Settings → Add-ons → Add-on Store → ⋯ → Check for updates**.
3. Find **Hatch Bridge** under "Local add-ons" and click **Install**.

### Option B — from this Git repository (shareable)

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋯ → Repositories**,
   paste this repo's URL.
2. Find **Hatch Bridge** in the store and install it.

## Configure

1. Open the app's **Configuration** tab and set `api_token` to a long random
   secret. Generate one with:

   ```bash
   openssl rand -hex 32
   ```

   The app refuses to start until this is set.
2. **Start** the app and check the **Log** tab. You should see
   `Hatch Bridge v1.0.0 listening on port 8099`.
3. On your LAN, open `http://homeassistant.local:8099` — you should see the
   "Hatch Bridge is running" page.

## Expose it securely (required for remote access)

The bridge speaks plain HTTP inside your network. **Do not port-forward port
8099 directly to the internet** — put TLS in front of it first. Pick one:

- **Recommended: Cloudflare Tunnel.** Install the `cloudflared` app, point a
  hostname (e.g. `ha-bridge.example.com`) at `http://homeassistant.local:8099`.
  No open ports, automatic TLS.
- **Reverse proxy:** the Nginx Proxy Manager app with a Let's Encrypt
  certificate, forwarding `https://ha-bridge.example.com` → `http://homeassistant.local:8099`.

Either way you end up with an `https://…` URL.

## Connect your AI assistant

Once the bridge is reachable at an `https://` URL:

1. Tell your assistant the public URL.
2. Store the `api_token` in your assistant's secure credential storage
   (never send it in plain chat).
3. Your assistant verifies with `GET /api/health`, then can read states and
   call services for you, e.g. "turn off all the lights downstairs" or
   "is the front door locked?".

## Security notes

- Anyone with the URL **and** the token can control your home. Treat the token
  like a password: generate a fresh one, don't share it, rotate it if exposed
  (change it in the Configuration tab and restart).
- Every service call is logged in the app's Log tab.
- The app talks to Home Assistant through the Supervisor proxy using the
  automatic `SUPERVISOR_TOKEN` — no HA long-lived token is stored anywhere.

## Files

```
hatch-ha-bridge/
├── repository.yaml          # add-on repository metadata
├── README.md                # this file
├── tests/
│   └── test_inbox.py        # inbox unit tests
└── hatch_bridge/            # the app itself
    ├── config.yaml          # app manifest (ports, options, permissions)
    ├── Dockerfile           # container build
    ├── requirements.txt     # Python dependencies
    ├── app.py               # the bridge (Flask + waitress)
    ├── DOCS.md              # docs shown in the app's Documentation tab
    └── translations/en.yaml # UI labels
```
