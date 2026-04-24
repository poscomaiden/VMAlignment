"""
VM Alignment Client — Windows Service Wrapper
Installs and runs the client as a Windows service using pywin32.

Usage:
    python service.py install    — Install the service
    python service.py start      — Start the service
    python service.py stop       — Stop the service
    python service.py remove     — Uninstall the service
    python service.py run        — Run in foreground (debug)
"""

import os
import sys
import time
import win32service
import win32serviceutil
import win32event
import servicemanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from client.client_service import VMAlignmentClient


class VMAlignmentService(win32serviceutil.ServiceFramework):
    _svc_name_ = "VMAlignmentClient"
    _svc_display_name_ = "VM Alignment Client"
    _svc_description_ = "Receives deployment updates via RabbitMQ and pulls from GitHub repos"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self._client = None
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self._stop_event)
        if self._client:
            self._client.stop()

    def SvcDoRun(self):
        servicemanager.LogInfoMsg(f"{self._svc_display_name_} starting")
        self._client = VMAlignmentClient()
        try:
            # Run the client in a thread so we can monitor the stop event
            import threading
            client_thread = threading.Thread(target=self._client.run, daemon=True)
            client_thread.start()

            # Wait for stop event
            win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)
        except Exception as e:
            servicemanager.LogErrorMsg(f"Service error: {e}")
        finally:
            if self._client:
                self._client.stop()
            servicemanager.LogInfoMsg(f"{self._svc_display_name_} stopped")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # Debug mode: show how to use
        print(f"VM Alignment Client Service")
        print(f"Usage: python {sys.argv[0]} [install|start|stop|remove|run]")
    else:
        win32serviceutil.HandleCommandLine(VMAlignmentService)
