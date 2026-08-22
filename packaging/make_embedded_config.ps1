$ErrorActionPreference = "Stop"
if (-not $env:DEEPSEEK_API_KEY) { throw "DEEPSEEK_API_KEY is not set" }
$bytes = [System.Text.Encoding]::UTF8.GetBytes($env:DEEPSEEK_API_KEY)
$encoded = [Convert]::ToBase64String($bytes)
Set-Content -Path embedded_config.py -Encoding UTF8 -Value ('DEEPSEEK_API_KEY_B64 = ' + ($encoded | ConvertTo-Json) + "`n")
