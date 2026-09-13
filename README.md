# prusalink-obico

A fork of [`moonraker-obico`](https://github.com/TheSpaghettiDetective/moonraker-obico)
that connects a **stock Prusa printer (Buddy firmware, via PrusaLink)** to a
self-hosted **Obico** server — no OctoPrint, no Klipper, no printer-attached Pi.

## How it works

`moonraker-obico`'s printer-facing half (a Moonraker WebSocket client) is
replaced by [`moonraker_obico/prusalink_conn.py`](moonraker_obico/prusalink_conn.py),
which **polls the PrusaLink HTTP API** (`/api/v1/status`, `/api/v1/job`) and
translates the response into the same Klipper/Moonraker-shaped status dict the
agent already consumes. The entire Obico-server-facing half (`server_conn`,
`PrinterState`, linking, webcam capture, tunnel) is unchanged — the connection
object keeps `id='moonrakerconn'` so the event routing is identical.

Print control (pause/resume/cancel) maps to PrusaLink job endpoints. Klipper-only
features with no PrusaLink equivalent (first-layer nozzle-cam AI, gcode terminal,
jog/home/set-temp, layer stats) are no-ops — spaghetti/failure detection, the
core feature, works fully.

## Config

`[prusalink]` section in `moonraker-obico.cfg` (replaces upstream `[moonraker]`):

```ini
[prusalink]
host = 192.168.1.36        # printer IP (DHCP-reserved)
port = 80
api_key = <PrusaLink API key: Settings > Network > PrusaLink>
# poll_interval = 2

[server]
url = http://obico-web.obico:3334   # in-cluster Obico server
# auth_token = <set by the link step>
```

## Run (container)

1. Link the printer to the Obico server (writes `auth_token` into the cfg):
   `python -m moonraker_obico.link -c /opt/printer_data/config/moonraker-obico.cfg`
   (enter the 6-digit code from the Obico dashboard → Link Printer).
2. Run the agent: the image's default CMD runs `moonraker_obico.app`.

Image is built to `ghcr.io/ebarrere/prusalink-obico:latest` by GitHub Actions.
Deployed in the homelab cluster (see the `obico` namespace manifests).

## Attribution

Derived from moonraker-obico (AGPL-3.0). See [LICENSE](LICENSE).
