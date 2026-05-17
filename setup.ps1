[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Write-Host "Step 1: Downloading portable Python..." -ForegroundColor Cyan
(New-Object System.Net.WebClient).DownloadFile("https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip", "C:\py311.zip")
Write-Host "Downloaded: $((Get-Item C:\py311.zip).Length) bytes" -ForegroundColor Green
Remove-Item "C:\Python311" -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive -Path "C:\py311.zip" -DestinationPath "C:\Python311" -Force
Write-Host "Step 2: Fixing Python config..." -ForegroundColor Cyan
$pth = Get-ChildItem "C:\Python311" -Filter "*._pth" | Select-Object -First 1 -ExpandProperty FullName
(Get-Content $pth) -replace "#import site","import site" | Set-Content $pth
Write-Host "Step 3: Installing pip..." -ForegroundColor Cyan
(New-Object System.Net.WebClient).DownloadFile("https://bootstrap.pypa.io/get-pip.py", "C:\Python311\get-pip.py")
& "C:\Python311\python.exe" "C:\Python311\get-pip.py" --quiet
Write-Host "Step 4: Installing deps..." -ForegroundColor Cyan
& "C:\Python311\python.exe" -m pip install MetaTrader5 anthropic pandas numpy ta python-dotenv schedule --quiet
Write-Host "All done! Launching Gyna..." -ForegroundColor Green
& "C:\Python311\python.exe" C:\Gyna\main_orchestrator.py
