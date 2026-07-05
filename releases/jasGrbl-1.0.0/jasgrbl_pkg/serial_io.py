"""Serial connection + GRBL streaming. Uses pyserial; degrades gracefully if absent.

Logging is delegated to a callback ``log(actor, message)`` so the Serial Log view is
the single source of truth. The background reader is paused while a stream is in
progress so the stream worker is the sole reader of the ``ok`` handshake replies.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import threading
import time
from typing import Callable, List, Optional, Tuple

LogFn = Callable[[str, str], None]
ProgressFn = Callable[[int, int], None]
DoneFn = Callable[[bool], None]

# --------------------------------------------------------------------------- #
# GRBL streaming tuning (character-counting protocol).
#
# GRBL 1.1's serial RX ring is RX_BUFFER_SIZE = 128 bytes (config.h). We keep the
# buffer as full as possible by tracking bytes in flight and only sending a line
# when it still fits under RX_LIMIT. RX_LIMIT is deliberately below 128: correct
# byte-counting (see _stream_worker) is what actually guarantees safety, and the
# extra margin is near-free insurance against a firmware build with a smaller
# buffer. This is GRBL's officially recommended streaming approach and is what
# keeps the 15-block planner fed so motion never stutters (unlike send-response,
# which leaves at most one block in flight and stalls between short segments).
# --------------------------------------------------------------------------- #
RX_BUFFER_SIZE = 128           # GRBL 1.1 serial RX ring size (config.h)
RX_LIMIT = 120                 # max bytes in flight; 8-byte safety margin under 128
STALL_WINDOW_S = 15.0          # abort if zero bytes received from GRBL for this long
POLL_INTERVAL_S = 0.25         # worker-emitted '?' status cadence during a stream (4 Hz)
PROGRESS_INTERVAL_S = 0.10     # min wall-clock gap between on_progress callbacks (~10 Hz)
HOLD_WAIT_S = 2.0              # max wait for GRBL to reach Hold/Idle after a feed-hold

# Realtime control bytes (bypass the RX buffer; never counted against RX_LIMIT).
RT_STATUS = b"?"
RT_FEED_HOLD = b"!"
RT_RESUME = b"~"
RT_SOFT_RESET = b"\x18"
RT_JOG_CANCEL = b"\x85"
RT_SPINDLE_STOP = b"\x9e"       # 0x9E: toggle spindle/laser stop (valid only while Hold)


def pyserial_available() -> bool:
    try:
        import serial  # noqa: F401
        return True
    except Exception:
        return False


def list_ports() -> List[Tuple[str, str]]:
    """Ports offered in the Connect drop-down, as (device, description) pairs.

    Besides the regular serial ports (pyserial's comports), some machines - notably
    the Refine LH721 vinyl cutter - enrol on the USB bus as a *printer-class* device
    rather than a CDC serial device. Those never appear in comports(), so we also scan
    the OS's printer device nodes / print spooler and merge them in (deduped by device).
    """
    seen: "dict[str, str]" = {}

    def add(device: str, desc: str) -> None:
        if device and device not in seen:
            seen[device] = desc or device

    # 1) Regular serial ports (the common case: USB-serial bridge -> /dev/cu.* / COMx).
    try:
        from serial.tools import list_ports as lp
        for p in lp.comports():
            add(p.device, p.description or p.device)
    except Exception:
        pass
    # 2) Printer-class / extra USB device nodes (the Refine LH721 case).
    for device, desc in _list_printer_ports():
        add(device, desc)
    return list(seen.items())


def _list_printer_ports() -> List[Tuple[str, str]]:
    """Best-effort enumeration of USB printer devices that comports() misses.

    Cross-platform and defensive: never raises. Covers Linux USB printer nodes,
    macOS device files pyserial can overlook, and CUPS-registered USB printers."""
    out: List[Tuple[str, str]] = []
    try:
        # Windows: a printer-class cutter (the Refine LH721 case) enrols on a USB print
        # port (USB001, ...) and never appears in comports(); surface it from the spooler.
        if sys.platform.startswith("win"):
            out.extend(_list_windows_usb_printers())
        # Linux exposes USB printers as character devices we can open directly.
        for path in sorted(set(glob.glob("/dev/usb/lp*") + glob.glob("/dev/lp*"))):
            out.append((path, "USB printer (%s)" % os.path.basename(path)))
        # macOS: a USB-serial cutter is normally a /dev/cu.* node (comports covers it),
        # but include tty.usb* as a fallback for ones pyserial does not surface.
        if sys.platform == "darwin":
            for path in sorted(glob.glob("/dev/tty.usb*")):
                out.append((path, "USB device (%s)" % os.path.basename(path)))
        # CUPS spooler (macOS/Linux): surface USB printers so a cutter that only
        # registers as a printer is still visible and selectable in the list.
        out.extend(_list_cups_usb_printers())
    except Exception:
        pass
    return out


def _windows_printers() -> List[Tuple[str, str]]:
    """(name, port) for every local USB* printer.

    Uses the Win32 spooler API (EnumPrinters) directly via ctypes - the same winspool.drv
    we spool through. This is deliberately NOT PowerShell: inside Inkscape's bundled Python
    the extension runs under pythonw (no console) with a rewritten PATH, so spawning
    ``powershell`` can fail outright, and ``Get-Printer`` can also stall for seconds probing
    an offline network/WSD printer and blow the timeout - either way the list would come
    back empty and the cutter would never appear. EnumPrinters is in-process, local-only and
    fast. Falls back to PowerShell only if the API call itself raises. Never raises."""
    try:
        printers = _enum_windows_printers()
    except Exception:
        printers = _windows_printers_powershell()
    return [(name, port) for name, port in printers
            if port and port.upper().startswith("USB")]


def _enum_windows_printers() -> List[Tuple[str, str]]:
    """All local printers as (name, port) via winspool EnumPrintersW level 2 (ctypes)."""
    import ctypes
    from ctypes import wintypes

    winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)

    class PRINTER_INFO_2(ctypes.Structure):
        # Field order/types must match the Win32 PRINTER_INFO_2W layout exactly so the
        # buffer strides correctly; only pPrinterName/pPortName are read.
        _fields_ = [
            ("pServerName", wintypes.LPWSTR), ("pPrinterName", wintypes.LPWSTR),
            ("pShareName", wintypes.LPWSTR), ("pPortName", wintypes.LPWSTR),
            ("pDriverName", wintypes.LPWSTR), ("pComment", wintypes.LPWSTR),
            ("pLocation", wintypes.LPWSTR), ("pDevMode", ctypes.c_void_p),
            ("pSepFile", wintypes.LPWSTR), ("pPrintProcessor", wintypes.LPWSTR),
            ("pDatatype", wintypes.LPWSTR), ("pParameters", wintypes.LPWSTR),
            ("pSecurityDescriptor", ctypes.c_void_p), ("Attributes", wintypes.DWORD),
            ("Priority", wintypes.DWORD), ("DefaultPriority", wintypes.DWORD),
            ("StartTime", wintypes.DWORD), ("UntilTime", wintypes.DWORD),
            ("Status", wintypes.DWORD), ("cJobs", wintypes.DWORD),
            ("AveragePPM", wintypes.DWORD),
        ]

    enum = winspool.EnumPrintersW
    enum.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD, wintypes.LPBYTE,
                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                     ctypes.POINTER(wintypes.DWORD)]
    enum.restype = wintypes.BOOL

    PRINTER_ENUM_LOCAL = 0x00000002
    level = 2
    needed = wintypes.DWORD(0)
    returned = wintypes.DWORD(0)
    # First call sizes the buffer (expected to "fail" with ERROR_INSUFFICIENT_BUFFER).
    enum(PRINTER_ENUM_LOCAL, None, level, None, 0,
         ctypes.byref(needed), ctypes.byref(returned))
    if needed.value == 0:
        return []
    buf = ctypes.create_string_buffer(needed.value)
    if not enum(PRINTER_ENUM_LOCAL, None, level,
                ctypes.cast(buf, wintypes.LPBYTE), needed.value,
                ctypes.byref(needed), ctypes.byref(returned)):
        return []
    info = ctypes.cast(buf, ctypes.POINTER(PRINTER_INFO_2))
    out: List[Tuple[str, str]] = []
    for i in range(returned.value):
        out.append((info[i].pPrinterName or "", info[i].pPortName or ""))
    return out


def _windows_printers_powershell() -> List[Tuple[str, str]]:
    """Fallback enumerator via PowerShell Get-Printer. (name, port); empty on any failure."""
    ps = "Get-Printer | ForEach-Object { \"$($_.Name)`t$($_.PortName)\" }"
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    out: List[Tuple[str, str]] = []
    for line in res.stdout.splitlines():
        name, sep, port = line.strip().partition("\t")
        if sep and name.strip():
            out.append((name.strip(), port.strip()))
    return out


def _list_windows_usb_printers() -> List[Tuple[str, str]]:
    """USB printers from the Windows spooler as (name, "Windows printer (PORT)") pairs.

    The printer *name* is the device id because Windows spooling addresses a printer by
    name, not by its USB* port; a cutter that enrols as a printer (the Refine LH721 case)
    is invisible to comports(). Selecting one routes sends through the print spooler as a
    RAW passthrough job (see _SpoolJob / resolve_spool_target) instead of pyserial."""
    return [(name, "Windows printer (%s)" % port) for name, port in _windows_printers()]


def resolve_spool_target(device: str) -> Optional[str]:
    """If ``device`` names a Windows USB printer - by printer name or by its USB* port -
    return the canonical printer name to spool to; otherwise None. Windows-only, so on
    every other platform (where the port really is an openable device node) this is a
    no-op and the caller falls through to the normal pyserial path."""
    if not device or not sys.platform.startswith("win"):
        return None
    dev = device.strip()
    for name, port in _windows_printers():
        if dev == name or dev == port:
            return name
    return None


class _SpoolJob:
    """Long-lived Windows printer HANDLE for a printer-class cutter, with ONE short RAW
    document per command (winspool.drv via ctypes).

    Design mirrors the proven Plotter reference: OpenPrinter once at connect and keep the
    HANDLE for the whole session, but open+close a print *document*
    (StartDocPrinter -> Write -> EndDocPrinter) on every send. This is essential - the
    spooler does NOT forward bytes to the usbprint device until EndDocPrinter is called, so a
    single session-long document would buffer every jog/reset forever and the head would
    never move. One short doc per command flushes it to the cutter immediately; the whole cut
    is sent as one such document. Windows-only; the constructor raises on failure. Not
    thread-safe: the caller serialises write_command()/close()."""

    def __init__(self, printer: str):
        import ctypes
        from ctypes import wintypes
        self._ct = ctypes
        self._printer = printer
        ws = ctypes.WinDLL("winspool.drv", use_last_error=True)
        self._ws = ws

        class DOC_INFO_1(ctypes.Structure):
            _fields_ = [("pDocName", wintypes.LPWSTR),
                        ("pOutputFile", wintypes.LPWSTR),
                        ("pDatatype", wintypes.LPWSTR)]
        self._DOC_INFO_1 = DOC_INFO_1

        ws.OpenPrinterW.argtypes = [wintypes.LPWSTR,
                                    ctypes.POINTER(wintypes.HANDLE), wintypes.LPVOID]
        ws.OpenPrinterW.restype = wintypes.BOOL
        ws.StartDocPrinterW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(DOC_INFO_1)]
        ws.StartDocPrinterW.restype = wintypes.DWORD
        ws.StartPagePrinter.argtypes = [wintypes.HANDLE]
        ws.StartPagePrinter.restype = wintypes.BOOL
        ws.WritePrinter.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD)]
        ws.WritePrinter.restype = wintypes.BOOL
        for fn in (ws.EndPagePrinter, ws.EndDocPrinter, ws.ClosePrinter):
            fn.argtypes = [wintypes.HANDLE]
            fn.restype = wintypes.BOOL

        self._h = wintypes.HANDLE()
        if not ws.OpenPrinterW(printer, ctypes.byref(self._h), None):
            self._h = None
            raise OSError("OpenPrinter failed for '%s' (Win32 error %d)"
                          % (printer, ctypes.get_last_error()))

    def write_command(self, data: bytes, abort: Optional[Callable[[], bool]] = None,
                      on_progress: Optional[ProgressFn] = None,
                      on_written: "Optional[Callable[[], None]]" = None,
                      doc_name: str = "jasGrbl", chunk_size: int = 65536) -> None:
        """Send one RAW document (StartDoc -> Write* -> EndDoc) so it flushes to the cutter
        now. Chunks the write, honouring abort()/on_progress. Raises OSError on failure.

        ``on_written`` (optional) fires once ALL bytes have been handed to the spooler, just
        before EndDocPrinter. This matters for a printer-class cutter: EndDocPrinter can block
        for a long time (or until the device drains) because such cutters do not report job
        completion, so the caller uses on_written to mark the send finished immediately
        (fire-and-forget) instead of waiting for EndDoc to return."""
        if self._h is None:
            raise OSError("printer handle is closed")
        ct = self._ct
        ws = self._ws
        info = self._DOC_INFO_1(doc_name, None, "RAW")
        if ws.StartDocPrinterW(self._h, 1, ct.byref(info)) == 0:
            raise OSError("StartDocPrinter failed for '%s' (Win32 error %d)"
                          % (self._printer, ct.get_last_error()))
        try:
            if not ws.StartPagePrinter(self._h):
                raise OSError("StartPagePrinter failed for '%s' (Win32 error %d)"
                              % (self._printer, ct.get_last_error()))
            total = len(data)
            sent = 0
            written = ct.c_ulong(0)
            step = max(1024, int(chunk_size))
            while sent < total:
                if abort is not None and abort():
                    break
                chunk = data[sent:sent + step]
                buf = ct.create_string_buffer(chunk, len(chunk))
                if not ws.WritePrinter(self._h, buf, len(chunk), ct.byref(written)):
                    raise OSError("WritePrinter failed for '%s' (Win32 error %d)"
                                  % (self._printer, ct.get_last_error()))
                sent += written.value or len(chunk)
                if on_progress:
                    on_progress(min(sent, total), total)
            if on_written:
                on_written()               # all bytes handed off; free the UI before EndDoc
            ws.EndPagePrinter(self._h)
        finally:
            ws.EndDocPrinter(self._h)

    def close(self) -> None:
        """Close the printer handle. Idempotent; never raises."""
        if self._h is None:
            return
        try:
            self._ws.ClosePrinter(self._h)
        except Exception:
            pass
        self._h = None


def _list_cups_usb_printers() -> List[Tuple[str, str]]:
    """USB printers known to CUPS, parsed from `lpstat -v`. Empty on any failure.

    Lines look like: ``device for LH721: usb://Refine/LH721?serial=...``. The usb://
    URI is not openable by pyserial, but listing it satisfies the requirement that a
    printer-recognised cutter still appear in the port list (see docs)."""
    try:
        res = subprocess.run(["lpstat", "-v"], capture_output=True, text=True, timeout=3)
    except Exception:
        return []
    printers: List[Tuple[str, str]] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line.lower().startswith("device for") or ":" not in line:
            continue
        name_part, uri = line.split(":", 1)          # first ':' is the printer-name delimiter
        name = name_part[len("device for"):].strip()
        uri = uri.strip()
        if uri.lower().startswith("usb"):
            printers.append((uri, "CUPS printer: %s" % name))
    return printers


def _run(cmd: List[str], timeout: float = 15.0) -> Tuple[Optional[int], str]:
    """Run a command, returning (returncode, combined output). returncode is None if
    the executable is missing or the run failed to start. Never raises."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.returncode, (res.stdout + res.stderr).strip()
    except FileNotFoundError:
        return None, "not found: %s" % cmd[0]
    except Exception as exc:  # timeout, permission, etc.
        return None, str(exc)


def clear_print_jobs() -> Tuple[bool, str]:
    """Force-cancel and reset every pending job in the OS print queue.

    A vinyl cutter enrolled as a printer can leave a job wedged in the spooler
    "forever"; this is the panic button that flushes it. Works without a serial
    connection (a wedged job is a print-system problem, not a link problem).

    Windows uses the Print Spooler; macOS and Linux use CUPS. Returns (ok, message)
    where message is a short human-readable summary for the log/dialog."""
    if sys.platform.startswith("win"):
        return _clear_print_jobs_windows()
    return _clear_print_jobs_cups()


def _clear_print_jobs_cups() -> Tuple[bool, str]:
    """CUPS (macOS + Linux): cancel + purge all jobs, then re-enable any queue a stuck
    job left paused/disabled. Job cancellation is user-level; the enable/accept steps
    may need admin and are best-effort."""
    lines: List[str] = []
    ran_any = False
    # 1) Cancel AND purge every job on every destination (-a all, -x purge data files).
    for cmd in (["cancel", "-a", "-x"], ["lprm", "-"]):
        rc, out = _run(cmd)
        if rc is not None:
            ran_any = True
        # lprm returns non-zero when the queue is already empty; that is not a failure.
        lines.append("%s: %s" % (cmd[0], "ok" if rc in (0, 1) else (out or "rc=%s" % rc)))
    # 2) A wedged job often leaves the printer disabled/rejecting - try to recover it.
    for name in _cups_printer_names():
        _run(["cupsenable", name])
        _run(["cupsaccept", name])
    if not ran_any:
        return False, "CUPS tools not found (cancel/lprm); cannot clear print jobs"
    return True, "Print jobs cleared (CUPS): " + "; ".join(lines)


def _cups_printer_names() -> List[str]:
    rc, out = _run(["lpstat", "-e"])
    if rc == 0 and out:
        return [ln.strip() for ln in out.splitlines() if ln.strip()]
    return []


def _clear_print_jobs_windows() -> Tuple[bool, str]:
    """Windows: clear stuck jobs by force-restarting the whole Print Spooler.

    The API approach (SetPrinter PURGE / SetJob DELETE) is unreliable for these USB cutters:
    a job that is already spooling wedges in a "deleting" state and the blade keeps running.
    So, like the proven reference tool, we brute-force it - stop the spooler, delete every
    spool file, restart the spooler - elevated via one UAC prompt. The caller disconnects the
    cutter first so no open handle keeps the service from stopping cleanly. Returns
    (ok, message); never raises."""
    return _restart_spooler_windows()


def _win_service_running(name: str) -> Optional[bool]:
    """True/False if the named Windows service is Running, else None if it can't be
    queried. Query-only (needs no elevation), all ctypes, never raises."""
    try:
        import ctypes
        from ctypes import wintypes

        class SERVICE_STATUS(ctypes.Structure):
            _fields_ = [("dwServiceType", wintypes.DWORD),
                        ("dwCurrentState", wintypes.DWORD),
                        ("dwControlsAccepted", wintypes.DWORD),
                        ("dwWin32ExitCode", wintypes.DWORD),
                        ("dwServiceSpecificExitCode", wintypes.DWORD),
                        ("dwCheckPoint", wintypes.DWORD),
                        ("dwWaitHint", wintypes.DWORD)]

        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.OpenSCManagerW.restype = wintypes.HANDLE
        adv.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        adv.OpenServiceW.restype = wintypes.HANDLE
        adv.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
        adv.QueryServiceStatus.restype = wintypes.BOOL
        adv.QueryServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
        adv.CloseServiceHandle.restype = wintypes.BOOL
        adv.CloseServiceHandle.argtypes = [wintypes.HANDLE]

        SC_MANAGER_CONNECT = 0x0001
        SERVICE_QUERY_STATUS = 0x0004
        SERVICE_RUNNING = 0x00000004

        scm = adv.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
        if not scm:
            return None
        try:
            svc = adv.OpenServiceW(scm, name, SERVICE_QUERY_STATUS)
            if not svc:
                return None
            try:
                st = SERVICE_STATUS()
                if not adv.QueryServiceStatus(svc, ctypes.byref(st)):
                    return None
                return st.dwCurrentState == SERVICE_RUNNING
            finally:
                adv.CloseServiceHandle(svc)
        finally:
            adv.CloseServiceHandle(scm)
    except Exception:
        return None


def _restart_spooler_windows() -> Tuple[bool, str]:
    """Force-restart the Windows Print Spooler, elevating through the native UAC prompt
    (ShellExecuteEx 'runas').

    Deliberately all-ctypes and PowerShell-free: inside Inkscape's bundled Python the
    extension runs under pythonw with a rewritten PATH, so spawning powershell/Start-Process
    is exactly what failed for the port list - the restart would silently do nothing. The
    elevated child gets a fresh standard environment (System32 on PATH), so we use the same
    plain command the proven reference tool uses, then verify the spooler's real state via
    the Service Control Manager. Returns (ok, message); never raises."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:
        return False, "ctypes unavailable; cannot restart the Print Spooler (%s)" % exc

    # Exactly the reference's ClearPrintQueue command: stop -> delete every spool file ->
    # start. '&' (not '&&') so each step runs even if the previous reports an error. Only the
    # del path is quoted; no outer wrapping (a single quoted arg is what cmd.exe /c expects).
    cmd_exe = "cmd.exe"
    params = (r'/c net stop spooler & del /q /f '
              r'"%systemroot%\System32\spool\PRINTERS\*.*" & net start spooler')

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NOASYNC = 0x00000100
    SW_HIDE = 0
    ERROR_CANCELLED = 1223

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = cmd_exe
    info.lpParameters = params
    info.nShow = SW_HIDE

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            return False, ("Print Spooler NOT restarted: Administrator elevation was "
                           "declined. Approve the UAC prompt, or run Inkscape as Admin.")
        return False, "Could not launch the elevated restart (Win32 error %d)." % err

    if info.hProcess:
        kernel32.WaitForSingleObject(info.hProcess, 60000)   # bounded wait (60 s)
        kernel32.CloseHandle(info.hProcess)

    running = _win_service_running("Spooler")
    if running is True:
        return True, "Print Spooler force-restarted; all queued jobs cleared."
    if running is False:
        return False, ("Print Spooler did not come back up - start the 'Print Spooler' "
                       "service manually (services.msc).")
    return True, ("Print Spooler restart was launched (final state unconfirmed). If a job "
                  "is still stuck, check services.msc.")


def parse_status(line: str) -> Optional[dict]:
    """Parse a GRBL 1.1 status report into a dict, or None if not a report.

    A report looks like ``<State|MPos:0.000,0.000,0.000|WCO:0.0,0.0,0.0|FS:0,0>`` or
    ``<Run|WPos:10.000,5.000,0.000|...>``. Returns e.g.
    {"state": "Idle", "MPos": [0.0, 0.0, 0.0], "WCO": [...], ...} - numeric fields become
    float lists; non-numeric fields are skipped."""
    line = line.strip()
    if len(line) < 3 or not (line.startswith("<") and line.endswith(">")):
        return None
    parts = line[1:-1].split("|")
    status = {"state": parts[0]}
    for token in parts[1:]:
        key, sep, val = token.partition(":")
        if not sep:
            continue
        nums = []
        for piece in val.split(","):
            try:
                nums.append(float(piece))
            except ValueError:
                nums = None
                break
        if nums is not None:
            status[key] = nums
    return status


def build_resume_preamble(program: List[str], resume_index: int
                          ) -> Tuple[Optional[List[str]], str]:
    """Reconstruct a SAFE preamble to resume a G-code program at ``resume_index``.

    G-code is stateful: units (G20/G21), distance mode (G90/G91), plane, work-coordinate
    system (G54..G59), feed F, and laser/spindle (M3/M4/M5 + S) are all sticky and were
    established by earlier lines. Re-sending from line N with GRBL's post-reset defaults
    would cut with the wrong units/feed or a live laser. This scans lines 0..N-1, tracks
    the last value of each modal group and the last commanded absolute XY, and returns a
    preamble that: restores the modal state, turns the laser/spindle OFF, rapids to the
    resume point with the beam off, then re-arms the laser/spindle at the point.

    Returns ``(preamble_lines, "")`` on success, or ``(None, reason)`` when a safe resume
    cannot be synthesised (e.g. the resume point lies in a G91 relative region or inside an
    arc, where the absolute restart position is ambiguous). The caller must confirm machine
    position (homing / manual zero) before using the preamble - this does not re-home."""
    import re
    prog = [ln.strip() for ln in program]
    n = max(0, min(resume_index, len(prog)))
    units = "G21"; distance = "G90"; plane = "G17"; feedmode = "G94"; wcs = "G54"
    feed = None; spindle = None; power = None
    x = y = z = None
    word = re.compile(r"([A-Za-z])\s*(-?\d+\.?\d*)")

    for line in prog[:n]:
        code = line.split(";", 1)[0].upper()
        toks = {m.group(1): m.group(2) for m in word.finditer(code)}
        gcodes = re.findall(r"G\s*(\d+\.?\d*)", code)
        mcodes = re.findall(r"M\s*(\d+)", code)
        for g in gcodes:
            gv = g.lstrip("0") or "0"
            if gv in ("20", "21"): units = "G" + gv
            elif gv in ("90", "91"): distance = "G" + gv
            elif gv in ("17", "18", "19"): plane = "G" + gv
            elif gv in ("93", "94"): feedmode = "G" + gv
            elif gv in ("54", "55", "56", "57", "58", "59"): wcs = "G" + gv
        for m in mcodes:
            if m in ("3", "4"): spindle = "M" + m
            elif m == "5": spindle = "M5"
        if "F" in toks: feed = toks["F"]
        if "S" in toks: power = toks["S"]
        if distance == "G90":
            if "X" in toks: x = toks["X"]
            if "Y" in toks: y = toks["Y"]
            if "Z" in toks: z = toks["Z"]
        else:
            # In relative mode we cannot know the absolute position from words alone.
            x = y = z = "__rel__"

    if x == "__rel__" or y == "__rel__":
        return None, ("resume point is in a relative-move (G91) region; absolute "
                      "restart position is ambiguous - choose an earlier safe point")
    if x is None or y is None:
        return None, "no absolute X/Y position established before the resume point"
    # Check the resume line itself is a clean start (not mid-arc: G2/G3 needs its own start).
    if n < len(prog):
        head = prog[n].split(";", 1)[0].upper()
        if re.search(r"G\s*0?[23]\b", head):
            return None, "resume point is inside an arc (G2/G3); choose an earlier point"

    pre: List[str] = [units, distance, plane, feedmode, wcs, "M5 S0"]
    if z is not None and z != "__rel__":
        pre.append("G0 Z%s" % z)             # lift/pos Z first with the tool off
    pre.append("G0 X%s Y%s" % (x, y))        # rapid to resume XY, beam/spindle OFF
    if feed is not None:
        pre.append("%s F%s" % (feedmode, feed))
    if spindle in ("M3", "M4"):
        pre.append("%s S%s" % (spindle, power if power is not None else "0"))
    return pre, ""


def _classify(text: str) -> str:
    t = text.strip().lower()
    if t.startswith("ok"):
        return "OK"
    if t.startswith("error"):
        return "ERROR"
    if t.startswith("alarm"):
        return "ALARM"
    if text.strip().startswith("Grbl"):
        return "GRBL"
    return "RX"


StatusFn = Callable[[str, float, float], None]


class SerialManager:
    def __init__(self, log: LogFn, on_status: Optional[StatusFn] = None):
        self._log = log
        # on_status(state, work_x, work_y) fires for each parsed GRBL status report.
        self._on_status = on_status
        self._ser = None
        # Printer-class transport: when connected to a Windows USB printer (e.g. the Refine
        # LH721 cutter) writes go through the print spooler (one RAW doc per command) instead
        # of pyserial. _spool_printer is the printer NAME; _spool_job holds the open printer
        # HANDLE (None = serial mode). _spool_lock serialises the GTK thread (jog/reset) and
        # the cut worker so their documents never interleave.
        self._spool_printer: Optional[str] = None
        self._spool_job: "Optional[_SpoolJob]" = None
        self._spool_lock = threading.RLock()
        self._reader: Optional[threading.Thread] = None
        self._reader_stop = threading.Event()
        self._stream_thread: Optional[threading.Thread] = None
        self._abort = threading.Event()
        self._streaming = False
        self.stop_on_error = True
        # Single-writer discipline: every self._ser.write() is serialized by this lock,
        # so a realtime byte (?, !, ~, 0x18) sent from another thread can never interleave
        # mid-line with the stream worker's line writes and corrupt a G-code command.
        self._io_lock = threading.RLock()
        # Set during disconnect so the stream worker's finally-block does NOT resurrect the
        # async reader on a port that is being torn down (avoids a reader-restart race).
        self._closing = threading.Event()
        # Pause/resume requests, serviced by the stream worker (sole writer during a stream).
        self._pause_req = threading.Event()
        self._resume_req = threading.Event()
        # Last GRBL state string seen (from '?' reports); used to pace feed-hold/abort.
        self._last_state = ""
        # Progress: index (into the streamed program) of the last line GRBL acked with 'ok'.
        # This is the ONLY trustworthy resume point - a sent-but-unacked line may not have run.
        self.last_acked_index = 0
        # GRBL status polling: send '?' periodically and parse the '<...>' reports for
        # the true work position. Only for GRBL machines (an HPGL cutter would choke on
        # a stray '?'), so main_window enables it via set_status_polling().
        self._poll_enabled = False
        self._status_thread: Optional[threading.Thread] = None
        self._status_stop = threading.Event()
        self._wco = [0.0, 0.0, 0.0]           # work-coordinate offset (WPos = MPos - WCO)

    # ---------------------------------------------------------- connection
    def is_connected(self) -> bool:
        if self._spool_job is not None:
            return True
        return self._ser is not None and getattr(self._ser, "is_open", False)

    def is_spooling(self) -> bool:
        """True when the active transport is the Windows print spooler (no serial link,
        no realtime channel, no per-line 'ok' handshake)."""
        return self._spool_job is not None

    def is_streaming(self) -> bool:
        return self._streaming

    def connect(self, port: str, baud: int, flow: str = "none") -> bool:
        """Open the port. ``flow`` selects serial flow control: 'software' (XON/XOFF),
        'hardware' (RTS/CTS) or 'none'. GRBL uses 'none' (it paces via its 'ok' ACK);
        HPGL cutters need software/hardware so the machine can throttle the host.

        If ``port`` resolves to a Windows USB printer (a printer-class cutter), there is no
        serial port to open: we enter spooler mode instead and route all writes through the
        Windows print spooler as RAW jobs. ``baud`` and ``flow`` are ignored in that mode."""
        if self.is_connected():
            self._log("WARN", "already connected")
            return True
        printer = resolve_spool_target(port)
        if printer:
            try:
                self._spool_job = _SpoolJob(printer)
            except Exception as exc:
                self._log("ERROR", "could not open printer '%s': %s" % (printer, exc))
                self._spool_job = None
                return False
            self._spool_printer = printer
            self._log("INFO",
                      "connected to '%s' via Windows print spooler (RAW)" % printer)
            return True
        if not pyserial_available():
            self._log("WARN", "pyserial is not installed - run: pip install pyserial")
            return False
        try:
            import serial
            self._ser = serial.Serial(
                port=port, baudrate=int(baud), timeout=0.15,
                xonxoff=(flow == "software"), rtscts=(flow == "hardware"))
        except Exception as exc:
            self._log("ERROR", "could not open %s @ %s: %s" % (port, baud, exc))
            self._ser = None
            return False
        flow_note = {"software": " (XON/XOFF)", "hardware": " (RTS/CTS)"}.get(flow, "")
        self._log("INFO", "connected to %s @ %s baud%s" % (port, baud, flow_note))
        self._start_reader()
        return True

    def disconnect(self) -> None:
        if self._spool_job is not None:
            # Stop feeding any in-flight cut, then close the printer handle (each command
            # already closed its own RAW document, so nothing is left half-open).
            self.abort_stream(hold=False)
            if self._stream_thread and self._stream_thread.is_alive():
                self._stream_thread.join(timeout=2.0)
            name = self._spool_printer
            with self._spool_lock:
                try:
                    self._spool_job.close()
                except Exception:
                    pass
                self._spool_job = None
            self._spool_printer = None
            self._log("INFO", "disconnected from '%s'" % name)
            return
        # Signal the stream worker to stop AND that we are tearing down, so its
        # finally-block does not restart the async reader on a closing port.
        self._closing.set()
        self.abort_stream()
        if self._stream_thread and self._stream_thread.is_alive():
            self._stream_thread.join(timeout=2.0)   # let the worker exit before we close
        self._stop_status_poll()
        self._stop_reader()
        if self._ser is not None:
            try:
                with self._io_lock:
                    self._ser.close()
            except Exception:
                pass
            self._ser = None
            self._log("INFO", "disconnected")
        self._closing.clear()

    # -------------------------------------------------------------- reader
    def _start_reader(self) -> None:
        self._reader_stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _stop_reader(self) -> None:
        self._reader_stop.set()
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=1.0)
        self._reader = None

    def _read_loop(self) -> None:
        while not self._reader_stop.is_set() and self.is_connected():
            try:
                raw = self._ser.readline()
            except Exception as exc:
                self._log("ERROR", "read error: %s" % exc)
                break
            if raw:
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                status = parse_status(text)
                if status is not None:
                    self._handle_status(status)
                    continue        # don't flood the Serial Log with 5 Hz status reports
                self._log(_classify(text), text)

    def _handle_status(self, status: dict) -> None:
        """Turn a parsed status report into a work-position callback. GRBL reports MPos
        or WPos (per $10); when only MPos is given, WPos = MPos - WCO, and WCO is sent
        only periodically, so we cache the last one."""
        self._last_state = status.get("state", "")
        if "WCO" in status and len(status["WCO"]) >= 2:
            self._wco = (status["WCO"] + [0.0, 0.0, 0.0])[:3]
        wpos = status.get("WPos")
        mpos = status.get("MPos")
        if wpos and len(wpos) >= 2:
            x, y = wpos[0], wpos[1]
        elif mpos and len(mpos) >= 2:
            x, y = mpos[0] - self._wco[0], mpos[1] - self._wco[1]
        else:
            return
        if self._on_status:
            self._on_status(status.get("state", ""), x, y)

    # -------------------------------------------------- status polling ('?')
    def set_status_polling(self, enabled: bool) -> None:
        """Enable/disable the periodic '?' status poll (GRBL only). Starts/stops the
        poller thread to match; safe to call before or after connect."""
        self._poll_enabled = enabled
        if enabled and self.is_connected():
            self._start_status_poll()
        else:
            self._stop_status_poll()

    def _start_status_poll(self) -> None:
        if self._status_thread and self._status_thread.is_alive():
            return
        self._status_stop.clear()
        self._status_thread = threading.Thread(target=self._status_loop, daemon=True)
        self._status_thread.start()

    def _stop_status_poll(self) -> None:
        self._status_stop.set()
        if self._status_thread and self._status_thread.is_alive():
            self._status_thread.join(timeout=1.0)
        self._status_thread = None

    def _status_loop(self) -> None:
        # ~5 Hz. Skip while streaming: the stream worker owns the port then, and GRBL is
        # busy consuming the program (jogging/status display is not needed mid-job).
        while not self._status_stop.is_set() and self.is_connected():
            if self._poll_enabled and not self._streaming:
                self._write_realtime(b"?")
            self._status_stop.wait(0.2)

    # ----------------------------------------------------------- low-level
    def _spool(self, data: bytes) -> bool:
        """Send one RAW document (single command) to the printer. Logs only on failure
        (callers that want a TX line log it themselves, matching the serial path)."""
        try:
            with self._spool_lock:
                if self._spool_job is None:
                    self._log("WARN", "not connected")
                    return False
                self._spool_job.write_command(bytes(data))
            return True
        except Exception as exc:
            self._log("ERROR", "print write failed: %s" % exc)
            return False

    def _write_line(self, line: str) -> bool:
        if self._spool_job is not None:
            return self._spool((line + "\n").encode("utf-8"))
        if not self.is_connected():
            self._log("WARN", "not connected")
            return False
        try:
            with self._io_lock:
                self._ser.write((line + "\n").encode("utf-8"))
                self._ser.flush()
            return True
        except Exception as exc:
            self._log("ERROR", "write error: %s" % exc)
            return False

    def _write_realtime(self, byte: bytes) -> None:
        if self._spool_job is not None:
            return          # a spooled printer has no realtime control channel
        if self.is_connected():
            try:
                with self._io_lock:
                    self._ser.write(byte)
                    self._ser.flush()
            except Exception:
                pass

    def send_line(self, line: str) -> None:
        """Manual command from the dialog input."""
        line = line.strip()
        if not line:
            return
        if self._write_line(line):
            self._log("TX", line)

    def send_raw(self, data: bytes, label: str = "") -> None:
        """Write raw bytes verbatim (no newline, no ok handshake).

        Used for HPGL / RS-232 control sequences that are not line-oriented GRBL
        commands (e.g. the ESC.R device reset on a vinyl cutter)."""
        if self._spool_job is not None:
            if self._spool(bytes(data)) and label:
                self._log("TX", label)
            return
        if not self.is_connected():
            self._log("WARN", "not connected")
            return
        try:
            with self._io_lock:
                self._ser.write(data)
                self._ser.flush()
            if label:
                self._log("TX", label)
        except Exception as exc:
            self._log("ERROR", "write error: %s" % exc)

    # --------------------------------------------------------- realtime ctl
    def feed_hold(self) -> None:
        """Pause. During a stream this requests a pause the worker services (feed-hold +
        laser-off + stop refilling); otherwise it just sends the realtime '!' byte."""
        if self._streaming:
            self._resume_req.clear()
            self._pause_req.set()
            self._log("INFO", "pause requested (feed hold)")
        else:
            self._write_realtime(RT_FEED_HOLD)
            self._log("INFO", "feed hold (!)")

    def resume(self) -> None:
        """Resume from a pause. During a stream the worker sends '~' and restarts the
        buffer fill; otherwise it just sends the realtime '~' byte."""
        if self._streaming:
            self._pause_req.clear()
            self._resume_req.set()
            self._log("INFO", "resume requested")
        else:
            self._write_realtime(RT_RESUME)
            self._log("INFO", "resume (~)")

    def is_paused(self) -> bool:
        return self._streaming and self._pause_req.is_set()

    def soft_reset(self) -> None:
        self._write_realtime(RT_SOFT_RESET)
        self._log("INFO", "soft reset (Ctrl-X)")

    def query_status(self) -> None:
        self._write_realtime(RT_STATUS)

    def jog_cancel(self) -> None:
        """GRBL 0x85 realtime: cancel an in-progress jog (used on button release)."""
        self._write_realtime(RT_JOG_CANCEL)

    # ----------------------------------------------------------- streaming
    def stream(self, lines: List[str], on_progress: Optional[ProgressFn] = None,
               on_done: Optional[DoneFn] = None,
               preamble: Optional[List[str]] = None) -> bool:
        """Stream a G-code program to GRBL using the character-counting protocol.

        ``preamble`` (optional) is a list of setup lines sent BEFORE the program to
        restore modal state on a resume-from-line-N (units/distance/feed/laser + a
        safe reposition rapid). Preamble lines are streamed with the same buffer
        accounting but are not counted in the program's progress/acked index."""
        if not self.is_connected():
            self._log("WARN", "cannot stream: not connected")
            if on_done:
                on_done(False)
            return False
        if self._streaming:
            self._log("WARN", "a stream is already running")
            return False
        self._abort.clear()
        self._pause_req.clear()
        self._resume_req.clear()
        self.last_acked_index = 0
        # The stream worker must own the port: pause the async reader first, and drop any
        # stale RX bytes that could be miscounted as acks and desync the FIFO.
        self._stop_reader()
        try:
            with self._io_lock:
                self._ser.reset_input_buffer()
        except Exception:
            pass
        self._streaming = True
        self._stream_thread = threading.Thread(
            target=self._stream_worker, args=(lines, on_progress, on_done, preamble),
            daemon=True)
        self._stream_thread.start()
        return True

    def abort_stream(self, hold: bool = True) -> None:
        """Stop the running stream. For a GRBL stream (``hold=True``) the stream worker
        owns the realtime sequence (feed-hold -> laser off -> soft reset) once it sees the
        abort flag, keeping the single-writer discipline. For an HPGL byte stream
        (``hold=False``) there is no GRBL realtime handling; the byte worker just stops."""
        if self._streaming:
            self._resume_req.clear()
            self._pause_req.clear()
            self._abort.set()

    def stream_bytes(self, data, on_progress: Optional[ProgressFn] = None,
                     on_done: Optional[DoneFn] = None, chunk_size: int = 512,
                     delay_s: float = 0.0) -> bool:
        """Stream a raw payload (e.g. an HPGL .plt) with NO per-line ACK, paced by flow
        control. Writes in chunks on a background thread; write() blocks while the cutter
        asserts XOFF/CTS, so flow control does the real throttling and ``delay_s`` is an
        extra safety pause between chunks. Progress is reported in bytes.

        In spooler mode the payload is written into the open session job instead (flow
        control / chunk pacing are the spooler's job), while still driving on_progress and
        the streaming state so the UI's Start/Stop and progress overlay behave the same."""
        if self._spool_job is not None:
            return self._spool_stream(data, on_progress, on_done)
        if not self.is_connected():
            self._log("WARN", "cannot send: not connected")
            if on_done:
                on_done(False)
            return False
        if self._streaming:
            self._log("WARN", "a stream is already running")
            return False
        self._abort.clear()
        self._stop_reader()                     # stream worker owns the port
        self._streaming = True
        self._stream_thread = threading.Thread(
            target=self._stream_bytes_worker,
            args=(data, on_progress, on_done, max(1, int(chunk_size)), max(0.0, delay_s)),
            daemon=True)
        self._stream_thread.start()
        return True

    def _stream_bytes_worker(self, data, on_progress, on_done, chunk_size, delay_s) -> None:
        raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
        total = len(raw)
        sent = 0
        ok = True
        try:
            while sent < total:
                if self._abort.is_set():
                    self._log("WARN", "send aborted by user")
                    ok = False
                    break
                chunk = raw[sent:sent + chunk_size]
                try:
                    with self._io_lock:
                        self._ser.write(chunk)  # blocks here while throttled (XOFF/CTS)
                        self._ser.flush()
                except Exception as exc:
                    self._log("ERROR", "write error: %s" % exc)
                    ok = False
                    break
                sent += len(chunk)
                if on_progress:
                    on_progress(sent, total)
                if delay_s:
                    time.sleep(delay_s)
        finally:
            self._streaming = False
            if not self._closing.is_set() and self.is_connected():
                self._start_reader()
        if ok and not self._abort.is_set():
            self._log("INFO", "send complete (%d bytes)" % sent)
        if on_done:
            on_done(ok and not self._abort.is_set())

    def _spool_stream(self, data, on_progress: Optional[ProgressFn],
                      on_done: Optional[DoneFn]) -> bool:
        """Spooler-mode counterpart of stream_bytes: write the payload into the open
        session job on a background thread, mirroring the streaming state so the UI is
        unchanged."""
        if self._streaming:
            self._log("WARN", "a stream is already running")
            return False
        self._abort.clear()
        self._streaming = True
        self._stream_thread = threading.Thread(
            target=self._spool_bytes_worker, args=(data, on_progress, on_done), daemon=True)
        self._stream_thread.start()
        return True

    def _spool_bytes_worker(self, data, on_progress, on_done) -> None:
        raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
        raw = bytes(raw)
        fired = {"done": False}

        def finish(ok: bool) -> None:
            # Runs exactly once. Marks the send finished (UI stops "sending") as soon as the
            # bytes are handed to the spooler, WITHOUT waiting for EndDocPrinter - which can
            # block indefinitely on a printer-class cutter that never reports job completion.
            if fired["done"]:
                return
            fired["done"] = True
            self._streaming = False
            aborted = self._abort.is_set()
            if aborted:
                self._log("WARN", "cut aborted by user")
            elif ok:
                self._log("INFO", "cut sent to '%s' (%d bytes)" % (self._spool_printer, len(raw)))
            if on_done:
                on_done(ok and not aborted)

        try:
            # Hold the lock for the whole cut so a stray jog write cannot interleave inside
            # the HPGL stream; abort is checked between chunks inside write_command().
            with self._spool_lock:
                if self._spool_job is None:
                    raise OSError("not connected")
                self._spool_job.write_command(
                    raw, abort=self._abort.is_set, on_progress=on_progress,
                    on_written=lambda: finish(True), doc_name="jasGrbl HPGL cut")
            finish(True)               # no-op if on_written already fired
        except Exception as exc:
            self._log("ERROR", "print write failed: %s" % exc)
            finish(False)

    @staticmethod
    def _prepare_program(lines: List[str]) -> List[str]:
        """Strip comments, blank lines, and stray CR so byte-counting is deterministic.

        A ';' or '(...)' comment still costs RX bytes and a round-trip if sent, so we
        drop them entirely. CR is stripped because we transmit a single '\\n' terminator
        and count exactly one terminator byte per line (a CRLF file would otherwise
        silently add a byte per line and desync the buffer accounting)."""
        out: List[str] = []
        for ln in lines:
            s = ln.strip().strip("\r").strip()
            if not s or s.startswith(";") or s.startswith("("):
                continue
            out.append(s)
        return out

    def _stream_worker(self, lines: List[str], on_progress, on_done,
                       preamble: Optional[List[str]] = None) -> None:
        """Character-counting streaming: keep GRBL's RX buffer full (never over RX_LIMIT),
        decrement in-flight bytes on each 'ok'/'error'. This keeps the planner fed for
        smooth continuous motion and runs the link near line-rate. The worker is the SOLE
        reader and writer of the port for the whole stream (single-writer discipline)."""
        pre = self._prepare_program(preamble) if preamble else []
        program = self._prepare_program(lines)
        # queue is the full byte-stream: preamble lines first, then the program. We track
        # how many acked lines fall in the program (past the preamble) for progress/resume.
        queue = pre + program
        total = len(program)
        pre_n = len(pre)
        payloads = [(ln + "\n").encode("utf-8") for ln in queue]
        lengths = [len(p) for p in payloads]

        from collections import deque
        pending = deque()          # byte-lengths of written-but-unacked lines (FIFO)
        pending_bytes = 0
        tx = 0                     # next queue index to send
        acked = 0                  # count of lines acked with ok/error
        ok_all = True
        rx = bytearray()           # accumulator: only complete '\n'-terminated lines parsed
        pausing = False            # feed-hold sent, waiting for GRBL to report Hold/Idle
        paused = False             # fully held (beam off); not refilling
        last_rx = time.monotonic()          # watchdog: last time ANY byte arrived
        last_poll = 0.0
        last_progress = 0.0

        def emit_progress(force=False):
            nonlocal last_progress
            if not on_progress:
                return
            now = time.monotonic()
            done = max(0, acked - pre_n)
            if force or now - last_progress >= PROGRESS_INTERVAL_S:
                last_progress = now
                on_progress(min(done, total), total)

        # Guard: a single line longer than the whole buffer can never be sent -> would
        # deadlock the fill test forever. Reject up front (real G-code never hits this).
        for i, L in enumerate(lengths):
            if L > RX_LIMIT:
                self._log("ERROR", "line too long for RX buffer (%d bytes): %s"
                          % (L, queue[i]))
                self._finish_stream(False, on_done)
                return

        try:
            while acked < len(queue):
                # ---- 1) service control requests (worker is the sole writer) ----------
                # Pause is a small state machine driven by the normal read loop below, so
                # the 'ok's still in flight keep being counted (a separate blocking wait
                # that swallowed them would desync the buffer accounting on resume).
                if self._abort.is_set():
                    self._log("WARN", "stream aborted by user")
                    self._abort_sequence()
                    ok_all = False
                    break
                if self._pause_req.is_set() and not paused and not pausing:
                    self._write_realtime(RT_FEED_HOLD)
                    self._log("INFO", "feed hold - pausing")
                    pausing = True
                if self._resume_req.is_set() and (paused or pausing):
                    self._write_realtime(RT_RESUME)
                    self._resume_req.clear()
                    self._log("INFO", "resumed")
                    paused = pausing = False

                # ---- 2) fill the RX buffer as far as RX_LIMIT allows (single burst) ----
                if not paused and not pausing:
                    wrote_any = False
                    while tx < len(queue) and pending_bytes + lengths[tx] <= RX_LIMIT:
                        try:
                            with self._io_lock:
                                self._ser.write(payloads[tx])
                            wrote_any = True
                        except Exception as exc:
                            self._log("ERROR", "write error: %s" % exc)
                            ok_all = False
                            self._abort.set()
                            break
                        pending.append(lengths[tx])
                        pending_bytes += lengths[tx]
                        tx += 1
                    if wrote_any:
                        try:
                            with self._io_lock:
                                self._ser.flush()
                        except Exception:
                            pass

                # ---- 3) periodic '?' for live position + watchdog liveness ------------
                now = time.monotonic()
                if now - last_poll >= POLL_INTERVAL_S:
                    self._write_realtime(RT_STATUS)
                    last_poll = now

                # ---- 4) drain replies; parse only COMPLETE lines ----------------------
                # Read exactly what's buffered (in_waiting) so acks drain the instant they
                # arrive; when idle, read(1) blocks up to the port timeout and paces the loop
                # (avoids a busy-spin) without delaying a burst of replies.
                try:
                    n = getattr(self._ser, "in_waiting", 0) or 0
                    chunk = self._ser.read(n if n else 1)
                except Exception as exc:
                    self._log("ERROR", "read error: %s" % exc)
                    ok_all = False
                    break
                if chunk:
                    last_rx = time.monotonic()
                    rx += chunk
                    while b"\n" in rx:
                        raw, _, rest = rx.partition(b"\n")
                        rx = bytearray(rest)
                        text = raw.decode("utf-8", "replace").strip()
                        if not text:
                            continue
                        low = text.lower()
                        if low == "ok":
                            if pending:
                                pending_bytes -= pending.popleft()
                            else:
                                self._log("WARN", "unexpected 'ok' (accounting desync)")
                            acked += 1
                            if acked > pre_n:
                                self.last_acked_index = acked - pre_n
                            emit_progress()
                        elif low.startswith("error"):
                            if pending:
                                pending_bytes -= pending.popleft()
                            acked += 1
                            self._log("ERROR", text)
                            if self.stop_on_error:
                                self._log("ERROR", "stopping stream on error")
                                ok_all = False
                                self._abort.set()   # top-of-loop runs the abort sequence
                                break
                        elif low.startswith("alarm"):
                            self._log("ALARM", text)
                            ok_all = False
                            self._abort.set()
                            break
                        elif text.startswith("<"):
                            st = parse_status(text)
                            if st is not None:
                                self._handle_status(st)
                                # Finish a pause once GRBL has actually stopped: kill the
                                # beam (0x9E) so a constant-power laser can't burn through
                                # while parked. Idle covers the buffer-empty no-op hold.
                                state = (self._last_state or "").lower()
                                if pausing and (state.startswith("hold")
                                                or state.startswith("idle")):
                                    self._write_realtime(RT_SPINDLE_STOP)
                                    pausing = False
                                    paused = True
                                    self._log("INFO", "paused (laser/spindle off)")
                        else:
                            # [MSG:...], [GC:...], Grbl banner, setting echoes - log, no pop.
                            self._log(_classify(text), text)

                # ---- 5) watchdog: dead link = zero bytes for STALL_WINDOW_S -----------
                # A genuinely long/slow move still answers our 4 Hz '?' polls, so RX never
                # goes silent on a live machine; silence means the link is gone.
                if not paused and not pausing and pending and \
                        time.monotonic() - last_rx > STALL_WINDOW_S:
                    self._log("ERROR", "no response for %.0fs - link stalled" % STALL_WINDOW_S)
                    ok_all = False
                    self._abort_sequence()
                    break

                if not chunk and not paused and tx >= len(queue):
                    # everything sent, waiting only on trailing acks: brief yield
                    time.sleep(0.002)
        finally:
            emit_progress(force=True)
            self._finish_stream(ok_all and not self._abort.is_set(), on_done)

    def _finish_stream(self, ok: bool, on_done) -> None:
        """Common stream teardown: always clear streaming state, and restart the async
        reader UNLESS we are disconnecting (guarded by _closing to avoid a restart race)."""
        self._streaming = False
        self._pause_req.clear()
        self._resume_req.clear()
        if ok:
            self._log("INFO", "stream complete (%d lines)" % self.last_acked_index)
        if not self._closing.is_set() and self.is_connected():
            self._start_reader()
        if on_done:
            on_done(ok)

    def _wait_for_hold(self) -> None:
        """Poll '?' until GRBL reports Hold (or Idle, if the buffer was already empty so the
        hold was a no-op). Bounded by HOLD_WAIT_S so we never block forever."""
        deadline = time.monotonic() + HOLD_WAIT_S
        while time.monotonic() < deadline:
            self._write_realtime(RT_STATUS)
            try:
                chunk = self._ser.read(128)
            except Exception:
                return
            if chunk:
                for raw in bytes(chunk).split(b"\n"):
                    text = raw.decode("utf-8", "replace").strip()
                    st = parse_status(text) if text.startswith("<") else None
                    if st is not None:
                        self._handle_status(st)
                state = (self._last_state or "").lower()
                if state.startswith("hold") or state.startswith("idle"):
                    return
            time.sleep(0.02)

    def _abort_sequence(self) -> None:
        """Stop a running job as safely as possible: feed-hold to a controlled stop
        (preserves position, unlike a reset mid-motion), kill the beam, then soft-reset to
        flush GRBL's planner + RX buffer. After a reset the RX buffer is empty, so any host
        accounting is stale and the caller discards it."""
        self._write_realtime(RT_FEED_HOLD)
        self._wait_for_hold()
        self._write_realtime(RT_SPINDLE_STOP)     # laser/spindle off before flushing
        self._write_realtime(RT_SOFT_RESET)       # flush planner + RX buffer
        self._log("INFO", "job stopped (feed-hold -> soft reset)")
