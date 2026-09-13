"""PrusaLink connection — drop-in replacement for MoonrakerConn.

Polls a Prusa MK-series printer's PrusaLink HTTP API and translates its JSON
into the moonraker-shaped status dict that the rest of the agent (PrinterState,
ServerConn, app.event_loop) already consumes. Keeps id='moonrakerconn' so
app.py's event routing is unchanged; the poll thread pushes the same
'status_update' events MoonrakerConn would.

Verified against MK4 firmware 6.5.7 / PrusaLink 2.1.2 (see docs-captured-payloads.txt).
"""
import logging
import threading
import time

import requests  # type: ignore

from .utils import run_in_thread
from .moonraker_conn import Event

_logger = logging.getLogger('obico.prusalink_conn')

# PrusaLink printer.state -> Klipper print_stats.state (what get_state_from_status reads)
_PRINT_STATE = {
    'IDLE': 'standby',
    'READY': 'standby',
    'FINISHED': 'complete',
    'STOPPED': 'cancelled',
    'PRINTING': 'printing',
    'PAUSED': 'paused',
    'ATTENTION': 'paused',
    'BUSY': 'printing',
    'ERROR': 'error',
}
_ACTIVE = ('PRINTING', 'PAUSED', 'ATTENTION', 'BUSY')

# Reported for both heaters when the printer is unreachable, so the temperature
# panel renders a value instead of spinning. Basement ambient ~20-25C.
_ROOM_TEMP_C = 21.0


class PrusaLinkConn:
    def __init__(self, config, sentry, on_event):
        self.id = 'moonrakerconn'
        self.app_config = config
        self.sentry = sentry
        self._on_event = on_event
        self.shutdown = False
        self.remote_event_handlers = {}
        self.poll_interval = float(getattr(config.moonraker, 'poll_interval', 0) or 2)
        self._cur_job_id = None
        self._job_start_ts = {}   # job_id -> epoch start (cached so current_print_ts is stable)

    # --- lifecycle ------------------------------------------------------
    def block_until_klippy_ready(self):
        # Do NOT block on printer reachability — PrusaLink runs on the printer,
        # which may be powered off. If we blocked here, a restart while the
        # printer is off would leave the agent stuck and never connect to Obico
        # (plugin shows offline). update_moonraker_objects only needs our stubs
        # (hardcoded heaters + cfg-file webcams), so proceed regardless; the poll
        # loop reports the printer offline until PrusaLink comes back.
        try:
            self._get('/api/version', timeout=5)
            _logger.info('PrusaLink reachable at startup')
        except Exception:
            _logger.info('PrusaLink not reachable at startup (printer off?); connecting to Obico anyway')
        self.app_config.update_moonraker_objects(self)
        run_in_thread(self._poll_loop)

    def add_remote_event_handler(self, event_name, handler):
        self.remote_event_handlers[event_name] = handler

    def push_event(self, event):
        if self.shutdown or not self._on_event:
            return
        self._on_event(event)

    def close(self):
        self.shutdown = True

    def ensure_api_key(self):
        pass  # PrusaLink api_key is supplied via config; nothing to fetch

    # --- HTTP helpers ---------------------------------------------------
    def _base(self):
        return self.app_config.moonraker.http_address()

    def _headers(self):
        key = self.app_config.moonraker.api_key
        return {'X-Api-Key': key} if key else {}

    def _get(self, path, timeout=8):
        r = requests.get(f'{self._base()}{path}', headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return r.json() if r.text.strip() else {}

    def _put(self, path, timeout=8):
        r = requests.put(f'{self._base()}{path}', headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return {}

    def _delete(self, path, timeout=8):
        r = requests.delete(f'{self._base()}{path}', headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return {}

    # --- polling --------------------------------------------------------
    def _poll_loop(self):
        while not self.shutdown:
            self._fetch_and_emit()
            time.sleep(self.poll_interval)

    def request_status_update(self, objects=None):
        # app.py calls this to force a fresh status; do one poll now.
        self._fetch_and_emit()

    def _fetch_and_emit(self):
        try:
            status = self._get('/api/v1/status')
        except Exception:
            # Printer powered off / PrusaLink unreachable. Report the PRINTER as
            # offline (webhooks.state != 'ready') rather than emitting
            # 'mr_disconnected' (which clears state -> empty status -> Obico shows
            # the *plugin* offline). This keeps the agent online with the printer
            # shown offline, matching moonraker-obico when Klipper is off.
            self._cur_job_id = None
            self.push_event(Event(
                sender=self.id, name='status_update',
                data={'result': {'status': self._offline_status()}},
            ))
            return
        try:
            klipper_status = self._translate(status)
            self.push_event(Event(
                sender=self.id, name='status_update',
                data={'result': {'status': klipper_status}},
            ))
        except Exception:
            self.sentry.captureException()

    def _offline_status(self):
        # PrusaLink unreachable (printer powered off). Keep the honest Offline
        # state (webhooks.state != 'ready' -> printer shown Offline), but report a
        # room-temp reading instead of 0.0 so the temperature panel has a value to
        # render instead of spinning "Loading temperature..." forever. (The temp
        # field must be numeric — it's round()'d and charted — so it can't be a
        # literal "offline" string.) ~room temp ≈ what the thermistors read when off.
        return {
            'webhooks': {'state': 'offline', 'state_message': 'PrusaLink unreachable (printer powered off?)'},
            'print_stats': {
                'state': 'standby', 'filename': '', 'message': '',
                'print_duration': 0.0, 'total_duration': 0.0, 'filament_used': 0.0,
                'info': {'total_layer': None, 'current_layer': None},
            },
            'virtual_sdcard': {'progress': 0.0, 'file_position': 0, 'is_active': False},
            'display_status': {'progress': 0.0, 'message': None},
            'gcode_move': {
                'speed_factor': 1.0, 'extrude_factor': 1.0,
                'gcode_position': [0, 0, 0], 'absolute_coordinates': True,
                'homing_origin': [0, 0, 0, 0],
            },
            'toolhead': {'position': [0, 0, 0, 0], 'homed_axes': ''},
            'fan': {'speed': 0.0},
            'extruder': {'temperature': _ROOM_TEMP_C, 'target': 0.0},
            'heater_bed': {'temperature': _ROOM_TEMP_C, 'target': 0.0},
        }

    def _translate(self, status):
        printer = status.get('printer', {}) or {}
        raw_state = printer.get('state', 'IDLE')
        ps_state = _PRINT_STATE.get(raw_state, 'standby')

        job = status.get('job') or {}
        job_id = job.get('id')
        filename = ''
        if raw_state in _ACTIVE and job_id is not None:
            self._cur_job_id = job_id
            time_printing = job.get('time_printing') or 0
            if job_id not in self._job_start_ts:
                self._job_start_ts[job_id] = int(time.time()) - int(time_printing)
            filename = self._job_filename(job_id)
        else:
            self._cur_job_id = None

        progress = float(job.get('progress') or 0) / 100.0
        time_printing = float(job.get('time_printing') or 0)

        def num(v, d=0.0):
            return d if v is None else v

        return {
            'webhooks': {'state': 'ready', 'state_message': ''},
            'print_stats': {
                'state': ps_state,
                'filename': filename,
                'message': '',
                'print_duration': time_printing,
                'total_duration': time_printing,
                'filament_used': 0.0,
                'info': {'total_layer': None, 'current_layer': None},
            },
            'virtual_sdcard': {
                'progress': progress,
                'file_position': 0,
                'is_active': ps_state == 'printing',
            },
            'display_status': {'progress': progress, 'message': None},
            'gcode_move': {
                'speed_factor': num(printer.get('speed'), 100) / 100.0,
                'extrude_factor': num(printer.get('flow'), 100) / 100.0,
                'gcode_position': [num(printer.get('axis_x')), num(printer.get('axis_y')), num(printer.get('axis_z'))],
                'absolute_coordinates': True,
                'homing_origin': [0, 0, 0, 0],
            },
            'toolhead': {
                'position': [num(printer.get('axis_x')), num(printer.get('axis_y')), num(printer.get('axis_z')), 0],
                'homed_axes': 'xyz',
            },
            'fan': {'speed': 1.0 if num(printer.get('fan_print')) > 0 else 0.0},
            'extruder': {
                'temperature': num(printer.get('temp_nozzle')),
                'target': num(printer.get('target_nozzle')),
            },
            'heater_bed': {
                'temperature': num(printer.get('temp_bed')),
                'target': num(printer.get('target_bed')),
            },
        }

    def _job_filename(self, job_id):
        try:
            job = self._get('/api/v1/job')
            f = (job or {}).get('file', {}) or {}
            return f.get('display_name') or f.get('name') or ''
        except Exception:
            return ''

    # --- discovery stubs (no Klipper/Moonraker equivalents) -------------
    def find_all_heaters(self):
        return {'available_heaters': ['extruder', 'heater_bed'], 'available_sensors': []}

    def find_all_thermal_presets(self):
        return []

    def find_all_installed_plugins(self):
        return []

    def find_most_recent_job(self):
        if self._cur_job_id is None:
            return None
        return {'start_time': self._job_start_ts.get(self._cur_job_id)}

    def macro_is_configured(self, macro_name):
        return False

    def set_macro_variables(self, macro_name, **kwargs):
        pass  # Klipper gcode macros — n/a for PrusaLink

    # --- generic REST surface used by app.py / passthru_targets ---------
    def api_get(self, mr_method, timeout=5, raise_for_status=True, **params):
        # Webcam auto-discovery: return None so config falls back to the cfg file.
        if mr_method.startswith('server.webcams') or mr_method.startswith('server.database'):
            return None
        if 'files/metadata' in mr_method.replace('.', '/'):
            return self._file_metadata(params.get('filename'))
        return None

    def api_post(self, mr_method, timeout=None, multipart_filename=None, multipart_fileobj=None, **post_params):
        m = mr_method.replace('.', '/')
        if self._cur_job_id is not None:
            if m == 'printer/print/pause':
                return self._put(f'/api/v1/job/{self._cur_job_id}/pause')
            if m == 'printer/print/resume':
                return self._put(f'/api/v1/job/{self._cur_job_id}/resume')
            if m == 'printer/print/cancel':
                return self._delete(f'/api/v1/job/{self._cur_job_id}')
        # server/database/item (printer_id store), file upload/start, etc. — no PrusaLink equivalent
        return {}

    def _file_metadata(self, filename):
        try:
            job = self._get('/api/v1/job')
            f = (job or {}).get('file', {}) or {}
            return {
                'size': f.get('size'),
                'modified': f.get('m_timestamp'),
                'estimated_time': None,
                'filename': f.get('name'),
            }
        except Exception:
            return {}

    # --- physical control (best-effort / unsupported) ------------------
    def request_jog(self, axes_dict, is_relative, feedrate):
        _logger.info('jog not supported over PrusaLink')
        return None

    def request_home(self, axes):
        _logger.info('home not supported over PrusaLink')
        return None

    def request_set_temperature(self, heater, target_temp):
        _logger.info('set_temperature not supported over PrusaLink')
        return None
