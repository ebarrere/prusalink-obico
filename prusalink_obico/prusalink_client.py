"""PrusaLink client — the printer-facing half of the moonraker-obico fork.

Replaces moonraker-obico's `moonraker_conn.py` (WebSocket JSON-RPC to Moonraker)
with polling against a Prusa MK4's PrusaLink HTTP API. The Obico-server-facing
half of the agent is unchanged; it consumes the OctoPrint-flavoured status dict
`to_octoprint_status()` produces here.

Field mapping verified against a live MK4 (firmware 6.5.7, PrusaLink 2.1.2):
see docs-captured-payloads.txt.
"""
from __future__ import annotations

import requests

# PrusaLink printer.state enum -> OctoPrint state text + flags.
# OctoPrint consumers (and Obico) key off flags.printing / flags.paused etc.
_STATE_MAP = {
    "IDLE":      ("Operational", dict(operational=True,  ready=True)),
    "READY":     ("Operational", dict(operational=True,  ready=True)),
    "FINISHED":  ("Operational", dict(operational=True,  ready=True)),
    "STOPPED":   ("Operational", dict(operational=True,  ready=True)),
    "PRINTING":  ("Printing",    dict(printing=True)),
    "PAUSED":    ("Paused",      dict(paused=True)),
    "ATTENTION": ("Paused",      dict(paused=True, error=False)),  # user intervention
    "BUSY":      ("Printing",    dict(printing=True, busy=True)),
    "ERROR":     ("Error",       dict(error=True, closedOnError=True)),
}

_ALL_FLAGS = (
    "operational", "printing", "paused", "pausing", "cancelling",
    "error", "ready", "busy", "sdReady", "closedOnError",
)


class PrusaLinkClient:
    def __init__(self, host: str, api_key: str, timeout: float = 6.0):
        # host like "http://192.168.1.36"
        self.base = host.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["X-Api-Key"] = api_key

    # --- reads -----------------------------------------------------------
    def get_status(self) -> dict | None:
        """GET /api/v1/status — state, temps, and (when active) job summary.
        Returns None if the printer is unreachable (powered off / DHCP change);
        callers treat None as 'idle, do nothing' rather than an error."""
        try:
            r = self.session.get(f"{self.base}/api/v1/status", timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            return None

    def get_job(self) -> dict | None:
        """GET /api/v1/job — full current-job detail incl. file refs.
        Empty body when idle -> None."""
        try:
            r = self.session.get(f"{self.base}/api/v1/job", timeout=self.timeout)
            r.raise_for_status()
            return r.json() if r.text.strip() else None
        except (requests.RequestException, ValueError):
            return None

    # --- actuation -------------------------------------------------------
    # NOTE: verify method (PUT vs POST) against the live printer with a safe
    # pause/resume before trusting cancel. Implemented per the v1 API spec.
    def pause(self, job_id: int):
        return self._job_cmd(job_id, "pause")

    def resume(self, job_id: int):
        return self._job_cmd(job_id, "resume")

    def cancel(self, job_id: int):
        r = self.session.delete(f"{self.base}/api/v1/job/{job_id}", timeout=self.timeout)
        r.raise_for_status()
        return r

    def _job_cmd(self, job_id: int, cmd: str):
        r = self.session.put(f"{self.base}/api/v1/job/{job_id}/{cmd}", timeout=self.timeout)
        r.raise_for_status()
        return r

    # --- translation seam ------------------------------------------------
    def to_octoprint_status(self, status: dict, job: dict | None) -> dict:
        """Build the OctoPrint-flavoured dict the Obico agent's server side
        expects (the same shape moonraker-obico's printer.to_status emits)."""
        p = status.get("printer", {})
        raw_state = p.get("state", "IDLE")
        text, on_flags = _STATE_MAP.get(raw_state, ("Operational", {"operational": True}))
        flags = {f: False for f in _ALL_FLAGS}
        flags.update(on_flags)

        js = status.get("job") or {}
        completion = js.get("progress")            # 0..100 (percent)
        print_time = js.get("time_printing")       # seconds elapsed
        print_time_left = js.get("time_remaining")  # seconds remaining

        job_block = None
        if job:
            f = job.get("file", {})
            job_block = {
                "file": {
                    "name": f.get("name"),
                    "path": f.get("path"),
                    "display": f.get("display_name", f.get("name")),
                },
                "estimatedPrintTime": (print_time or 0) + (print_time_left or 0),
            }

        return {
            "state": {"text": text, "flags": flags},
            "job": job_block,
            "progress": None if completion is None else {
                "completion": completion,
                "printTime": print_time,
                "printTimeLeft": print_time_left,
            },
            "temperature": {
                "tool0": {
                    "actual": p.get("temp_nozzle"),
                    "target": p.get("target_nozzle"),
                },
                "bed": {
                    "actual": p.get("temp_bed"),
                    "target": p.get("target_bed"),
                },
            },
            # extras Obico can use for context
            "_prusalink": {
                "job_id": js.get("id"),
                "flow": p.get("flow"),
                "speed": p.get("speed"),
                "z": p.get("axis_z"),
            },
        }

    @staticmethod
    def is_active(status: dict | None) -> bool:
        if not status:
            return False
        return status.get("printer", {}).get("state") in ("PRINTING", "PAUSED", "ATTENTION")
