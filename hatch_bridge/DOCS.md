# Hatch Bridge

Token-protected API bridge between your Hatch AI assistant and Home Assistant.

## Configuration

### Option: `api_token`

A long random secret (minimum: don't leave it empty). Your Hatch assistant
sends it as `Authorization: Bearer <api_token>` on every API call.

Generate one:

```bash
openssl rand -hex 32
```

The app will not start until this is set. If the token ever leaks, change it
here, restart the app, and give the new token to your assistant.

### Option: `log_level`

How chatty the app log is: `debug`, `info` (default), `warning`, `error`.

### Option: `assistant_name`

The name of your AI assistant (default `Melnik`). It is used for the
auto-created prompt helper's friendly name, the persistent notification
title, and the default helper entity IDs (`input_text.<name>_prompt` /
`input_text.<name>_response`, lowercased). Change it to match your own
assistant; existing installs keep working because their explicit
`prompt_entity_id` / `response_entity_id` values are unchanged.

### Option: `prompt_entity_id`

The `input_text` helper the bridge watches for prompts
(default `input_text.melnik_prompt`). The bridge tries to create it
automatically on startup; if that fails, create it by hand: **Settings →
Devices & services → Helpers → Create helper → Text**, name it
"<assistant_name> prompt".

### Option: `response_entity_id`

The `input_text` helper the assistant's reply is mirrored into
(default `input_text.melnik_response`), if it exists. Optional.

### Option: `inbox_poll_secs`

Seconds between checks of the prompt helper (1–60, default 3).

## Prompt inbox

Anything typed into the prompt helper — from a dashboard text card or from
an automation calling `input_text.set_value` — is queued in the bridge's
inbox. The assistant's watcher picks new prompts up (typically within
~15 seconds), acts on them through this bridge, and reports the result back
as a Home Assistant persistent notification titled with your
`assistant_name` (plus the response helper, if it exists).

Example automation — ask the assistant to set an away scene when everyone
leaves:

```yaml
automation:
  - alias: "Ask Melnik for the away scene"
    trigger:
      - platform: state
        entity_id: zone.home
        to: "0"
    action:
      - service: input_text.set_value
        target:
          entity_id: input_text.melnik_prompt
        data:
          value: >-
            Everyone just left the house. Turn off all the lights except
            the hallway, and set the Dyson purifier to Normal.
```

## Network

The app listens on port **8099** inside the container, mapped to port **8099**
on the host. On your LAN it is reachable at `http://homeassistant.local:8099`.

> Do not expose port 8099 directly to the internet. Put a TLS-terminating
> reverse proxy (e.g. Nginx Proxy Manager with Let's Encrypt) or a Cloudflare
> Tunnel in front of it first.

## API reference

All routes return JSON. All `/api/*` routes require the bearer token.

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/ping` | Liveness probe, no auth (used by the Supervisor watchdog) |
| GET | `/` | Human-readable status page, no auth |
| GET | `/api/health` | Bridge + Home Assistant reachability |
| GET | `/api/states` | All entity states; `?domain=light` filters by domain |
| GET | `/api/states/<entity_id>` | Single entity state |
| GET | `/api/services` | Available service domains and services |
| POST | `/api/services/<domain>/<service>` | Call a service; JSON body becomes service data |
| GET | `/api/config` | Home Assistant configuration info |
| GET | `/api/inbox/status` | **Public, no auth:** pending prompt count + newest id (no content) |
| GET | `/api/inbox` | Pending text prompts queued from Home Assistant |
| POST | `/api/inbox/ack` | Mark a prompt handled: `{"id": "…", "response": "…"}` |

Example — turn on a light:

```bash
curl -X POST https://ha-bridge.example.com/api/services/light/turn_on \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "light.living_room"}'
```

## Troubleshooting

- **App won't start, log says `api_token` is empty:** set the token in the
  Configuration tab and restart.
- **`home_assistant_unreachable`:** the Supervisor proxy isn't responding;
  restart the app, and make sure Home Assistant Core is running.
- **`401 unauthorized`:** the `Authorization: Bearer …` header is missing or
  the token doesn't match the configured one.
- **Rebuilding after editing:** the Supervisor caches the manifest — use
  **Add-on Store → ⋯ → Check for updates** before reinstalling.
- **Log says `s6-overlay-suexec: fatal: can only run as pid 1`:** this was a
  conflict between the Home Assistant base image's s6-overlay init and the
  init process the Supervisor starts app containers with. Since v1.0.0 the
  app builds on plain `python:3.13-alpine` instead, so s6-overlay is not
  involved at all. If you see this error, you are running an older build —
  re-download the package and rebuild.
- **Reinstalling from scratch:** if a rebuild keeps failing, uninstall the
  app and install it again rather than rebuilding — this clears any cached
  container state.
