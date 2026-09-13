#!/bin/bash
# PrusaLink build: no Klipper gcode macros to install; just run the agent.
echo "ENTRYPOINT: Starting prusalink-obico"
exec /opt/venv/bin/python -m moonraker_obico.app "$@"
