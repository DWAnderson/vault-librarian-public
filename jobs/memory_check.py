#!/usr/bin/env python3
"""Memory check for the nightly report (2026-11-19).

The 2026-11-19 nightly failed because another program (Dell SupportAssist Remediation) had leaked ~34.5 GB of
committed memory, more than this PC's commit limit, so find_duplicates, the search index and the Claude CLI could
not get memory. This check runs at the start of the nightly and says plainly how much memory is committed, which
programs hold the most, and whether Windows logged low-memory warnings (System event 2004) in the last 24 hours,
so a failure like that names its cause in the report instead of looking like a vault bug.

  check(cfg, log) -> (text, needs, low)   low=True when memory is short enough to make tonight's steps fail

Thresholds: config nightly.memory {commit_warn_pct (85), process_warn_gb (4), top (5)}.
Read only; never raises (a failed check reports itself and returns no needs).
"""
import ctypes
import json
import os
import subprocess

GB = 1024 ** 3


class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _status():
    s = _MemStatus()
    s.dwLength = ctypes.sizeof(_MemStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
        raise OSError("GlobalMemoryStatusEx failed")
    return s


_PS = r"""
$p = Get-Process | Sort-Object PrivateMemorySize64 -Descending | Select-Object -First TOPN |
  ForEach-Object { [pscustomobject]@{ name = $_.ProcessName; id = $_.Id; private = $_.PrivateMemorySize64 } }
$since = (Get-Date).AddHours(-24); $boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
if ($boot -gt $since) { $since = $boot }   # warnings from before a reboot describe memory that is gone now
$e = @(Get-WinEvent -FilterHashtable @{LogName='System'; Id=2004; StartTime=$since} -ErrorAction SilentlyContinue)
$last = if ($e.Count) { ($e[0].Message -split "`r?`n" | Where-Object { $_ -match 'consumed' } | Select-Object -First 1) } else { '' }
[pscustomobject]@{ procs = @($p); events = $e.Count; last_at = $(if ($e.Count) { $e[0].TimeCreated.ToString('s') } else { '' }); last = "$last".Trim() } | ConvertTo-Json -Depth 4 -Compress
"""


def _processes_and_events(top):
    p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS.replace("TOPN", str(int(top)))],
                       capture_output=True, text=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (p.stdout or "").strip()
    if not out:
        raise RuntimeError((p.stderr or "no output").strip()[-300:])
    return json.loads(out)


def check(cfg, log=None):
    opts = (cfg.get("nightly") or {}).get("memory") or {}
    warn_pct = float(opts.get("commit_warn_pct", 85))
    proc_gb = float(opts.get("process_warn_gb", 4))
    top = int(opts.get("top", 5))
    if os.name != "nt":
        return "not checked (not on Windows)", [], False
    try:
        s = _status()
        limit, used = s.ullTotalPageFile, s.ullTotalPageFile - s.ullAvailPageFile
        pct = 100.0 * used / limit if limit else 0.0
        lines = [f"committed {used / GB:.1f} of {limit / GB:.1f} GB ({pct:.0f}%); "
                 f"RAM free {s.ullAvailPhys / GB:.1f} of {s.ullTotalPhys / GB:.1f} GB"]
        needs, low = [], pct >= warn_pct
        try:
            pe = _processes_and_events(top)
            procs = pe.get("procs") or []
            if isinstance(procs, dict):
                procs = [procs]
            lines.append("most committed: " + ", ".join(f"{x['name']} {x['private'] / GB:.1f} GB" for x in procs))
            big = [x for x in procs if x["private"] / GB >= proc_gb]
            if pe.get("events"):
                lines.append(f"Windows low-memory warnings in the last 24 h (since boot): {pe['events']} "
                             f"(latest {pe.get('last_at', '')}: {pe.get('last', '')[:220]})")
                low = True
            if big:
                names = ", ".join(f"{x['name']} ({x['private'] / GB:.1f} GB)" for x in big)
                needs.append(f"memory: {names} holds an unusually large amount of memory; "
                             f"restarting or removing it may be needed before tonight's steps can run")
        except Exception as e:  # noqa: BLE001
            lines.append(f"process list unavailable: {type(e).__name__}: {e}")
        if low and not needs:
            needs.append(f"memory is short ({pct:.0f}% committed"
                         + (", low-memory warnings in the last 24 h" if any("warnings" in ln for ln in lines) else "") + ")")
        if log:
            log("memory: " + " | ".join(lines))
        return "\n".join(lines), needs, low
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"memory check failed: {e}")
        return f"check failed: {type(e).__name__}: {e}", [], False


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import common as C  # noqa: E402
    txt, nd, lo = check(C.load_config())
    print(txt)
    for n in nd:
        print("NEEDS:", n)
    print("low:", lo)
