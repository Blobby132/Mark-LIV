# Notes for Claude

## Giving the user a download command

The user runs Windows PowerShell. Whenever they ask how to download a
branch (or you hand over finished work), give ONE PowerShell block they
paste all at once, in this shape:

```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$zip = "$env:TEMP\mark-liv.zip"
$out = "$HOME\Mark-LIV-download"
Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/Blobby132/Mark-LIV/archive/refs/heads/<branch>.zip" -OutFile $zip
Expand-Archive $zip -DestinationPath $out -Force
Set-Location "$out\Mark-LIV-<branch with / replaced by ->"
(Get-FileHash .\mods\markliv-bridge-1.0.0.jar -Algorithm SHA256).Hash
# expected: <the jar's sha256, upper case>
```

- Write the full URL out literally. Never build it from a variable such as
  `$branch`: if that line is not run in the same window the URL ends in
  `/heads/.zip` and GitHub answers "page not found".
- Keep the TLS 1.2 line and `-UseBasicParsing`: older Windows PowerShell
  needs both.
- Check the link downloads before giving it.
- Give the same URL as a plain browser link underneath, as a fallback.
- Include the `Get-FileHash` line with the expected hash, and when the jar
  changed, say to run `.\install_mod.bat` with Minecraft closed.
