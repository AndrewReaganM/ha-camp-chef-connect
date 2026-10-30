# Camp Chef Connect for Home Assistant

Unofficial Home Assistant integration for Camp Chef WiFi pellet grills (Woodwind,
SmokePro, Apex and other Gen 2 "Camp Chef Connect" controllers), using the same
cloud service as the Camp Chef Connect app.

Not affiliated with or endorsed by Camp Chef or Vista Outdoor.

## Why cloud?

The grill's WiFi module listens on no local ports at all; it only makes an
outbound connection to Camp Chef's servers. The only local alternative is
Bluetooth (see [doggkruse/ha-campchef](https://github.com/doggkruse/ha-campchef)),
which needs a Bluetooth adapter or ESPHome proxy within range of the grill.

## What you get

| Entity | Notes |
| --- | --- |
| Climate | Chamber temperature, set point, shut down (off). |
| Chamber / set temperature | Sensors, °F (converted to your unit system). |
| Probe 1–4 | Unknown when no probe is plugged in. 2-probe models get 2. |
| Pellet level | As reported by the controller. |
| Mode | standby, run, high, feed, shutdown fan, cook program. |
| Smoke level | 1–10, adjustable while running. |
| Fault / Last fault | Flame out, over temperature, sensor error. |
| Connected, WiFi signal, Transitioning | Diagnostics (the last two disabled by default). |

### Deliberate limits

- **No remote ignition.** Setting the climate to heat while the grill is off is
  refused. Light the grill at the controller; after that you can change the
  temperature and smoke level and shut it down from Home Assistant.
- Shutting down sends the controller into its normal shutdown cycle (fan runs
  to burn off the firepot, then standby).

## Install (HACS)

1. HACS → ⋮ → Custom repositories → add this repository's URL, category **Integration**.
2. Install **Camp Chef Connect** and restart Home Assistant.
3. Settings → Devices & services → Add integration → **Camp Chef Connect**.
4. Enter the email address you use in the Camp Chef Connect app. Camp Chef
   emails you a one-time PIN; enter it to finish.

If you signed in to the app with Apple using "Hide my email", use the
`@privaterelay.appleid.com` address shown in your Apple ID settings.

## How it works

The Camp Chef cloud (`api.campchef.app`, a Lightning Server backend) relays the
grill's Bluetooth characteristics over a websocket (`ws.api.campchef.app`,
channel `/client-socket/<grillId>`). Each frame is `[characteristic, op, payload]`
base64-encoded, with the same payload encoding as the grill's BLE interface.
The integration keeps one channel open per grill, receives pushed updates, and
re-reads telemetry every 60 seconds. The session token is refreshed
automatically; if Camp Chef rejects it, Home Assistant asks you to log in again.

## Development

```bash
pip install pytest-homeassistant-custom-component pytest-timeout
python -m pytest tests
```

`tests/test_protocol.py` covers the wire format; `tests/test_integration.py`
runs the config flow and entities against a fake cloud on localhost.
