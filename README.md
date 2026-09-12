# prusalink-obico

A fork/adaptation of [`moonraker-obico`](https://github.com/TheSpaghettiDetective/moonraker-obico)
that connects a **stock Prusa MK4 (Buddy firmware) to a self-hosted Obico server
over PrusaLink** — no OctoPrint, no Klipper, no printer-attached Raspberry Pi.

## Why

Obico gives you self-hosted, local-ML print-failure detection, but its clients
(`obico-for-octoprint`, `moonraker-obico`) require OctoPrint or Klipper. A stock
MK4 speaks neither — it exposes **PrusaLink** (HTTP API). Rather than bolt a Pi +
OctoPrint onto the printer, we swap moonraker-obico's *printer-facing half* for a
PrusaLink client and keep its *Obico-server-facing half* untouched.

## Architecture

```
 MK4 (Buddy fw)            this agent (a pod)                Obico server (in-cluster)
 ┌──────────┐   PrusaLink   ┌───────────────────┐   WSS      ┌──────────────────┐
 │ PrusaLink │◀── HTTP poll ─│ prusalink_client  │──────────▶│ web/tasks/ml_api │
 │  :80      │──── pause ───▶│  → OctoPrint dict │  status   │  (YOLO detection)│
 └──────────┘                │ (unchanged Obico  │  + jpegs  └──────────────────┘
 DCS-930L cam ── snapshot ──▶│  server client)   │◀ commands  (pause/cancel back)
```

The seam is moonraker-obico's OctoPrint-flavoured status dict. As long as we emit
that shape, the entire Obico-server side (`server_conn.py`, webcam capture, tunnel,
Janus) is reused verbatim.

## What changes vs. upstream moonraker-obico

| Upstream module | Fate |
|---|---|
| `moonraker_conn.py` (WS JSON-RPC) | **replaced** by `prusalink_obico/prusalink_client.py` (HTTP polling) |
| `printer.py` parsers (`to_status`, state machine) | **rewritten** — parse PrusaLink JSON, emit same dict |
| `passthru_targets.py` (job control) | pause/resume/cancel → PrusaLink job endpoints; jog/home/set-temp → unsupported |
| `config.py` `[moonraker]` | → `[prusalink] host + api_key`; webcam auto-discovery dropped (manual URLs) |
| `server_conn.py`, `webcam_*.py`, `tunnel.py`, `janus*` | **unchanged** |
| `nozzlecam.py` (first-layer AI), layer macros, timelapse-pause | **dropped** (Klipper-macro-only, no PrusaLink path) |

Push→poll: Moonraker pushes over WS; PrusaLink only polls. Upstream already treats
pushes as "go re-poll" and polls every 2s, so a poll loop reproduces its behaviour
(cost: ~1-2s latency on state transitions — irrelevant for failure alerts).

## Status field mapping (verified against live MK4 fw 6.5.7 / PrusaLink 2.1.2)

See `docs-captured-payloads.txt` for the raw idle + active-print responses.

| Obico needs | PrusaLink source |
|---|---|
| print state/flags | `/api/v1/status .printer.state` → OctoPrint flags (see `prusalink_client._STATE_MAP`) |
| progress %, time left/elapsed | `/api/v1/status .job.{progress,time_remaining,time_printing}` |
| job file / id | `/api/v1/job .{id,file}` |
| nozzle/bed temps | `/api/v1/status .printer.{temp_nozzle,target_nozzle,temp_bed,target_bed}` |
| pause/resume/cancel | `PUT /api/v1/job/{id}/pause|resume`, `DELETE /api/v1/job/{id}` |
| webcam frames | DCS-930L `/image.jpg` directly (Obico never got pixels from Moonraker) |

## TODO
- [ ] Graft `prusalink_client` into the upstream tree (replace `moonraker_conn`, rewire `app.py` event loop to poll).
- [ ] Rewrite `printer.py` parsers to call `to_octoprint_status`.
- [ ] Config: `[prusalink]` section; manual webcam URLs.
- [ ] **Verify pause/resume method (PUT vs POST) + cancel against the live printer** (safe pause/resume test).
- [ ] Rebuild the `current_print_ts` session identity off PrusaLink job `id`.
- [ ] Dockerfile + GitHub Actions → `ghcr.io/ebarrere/prusalink-obico`.
- [ ] k8s Deployment (in the homelab manifests repo) linking to the in-cluster Obico server.
