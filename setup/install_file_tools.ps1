# Installs the file-handling tools for the intake queue (plan 2026-09-28, phase 2, decision 9.2).
# Run once on this PC from your own Terminal (not from a Claude session: the Claude app's sandbox
# redirects writes under AppData\Local). Safe to run again.
#   Tesseract OCR                - needed by OCRmyPDF (text layer for scanned PDFs)
#   Calibre                      - ebook-convert for .mobi / .epub / .azw3
#   Python packages (runtime)    - ocrmypdf pymupdf markitdown extract-msg chardet pywin32
param([string]$Runtime = (Join-Path $env:LOCALAPPDATA 'Programs\VaultLibrarian\runtime'))
$ErrorActionPreference = 'Continue'
$log = Join-Path (Split-Path $PSScriptRoot) ('reports\install_file_tools-' + (Get-Date -Format 'yyyyMMdd-HHmm') + '.log')
Start-Transcript -Path $log | Out-Null

# Ghostscript is not in winget (2026-09-28) and isn't needed: the queue runs OCRmyPDF with
# --output-type pdf; Ghostscript is only used for PDF/A output.
foreach ($id in 'UB-Mannheim.TesseractOCR', 'calibre.calibre') {
    Write-Host "== winget $id"
    winget install -e --id $id --accept-package-agreements --accept-source-agreements --silent
}

# Put Tesseract, Ghostscript and Calibre on the user PATH (scheduled tasks pick it up at next logon)
$add = @('C:\Program Files\Tesseract-OCR', 'C:\Program Files\Calibre2')
$gs = Get-ChildItem 'C:\Program Files\gs' -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
if ($gs) { $add += (Join-Path $gs.FullName 'bin') }
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
foreach ($d in $add) {
    if ((Test-Path $d) -and ($userPath -notlike "*$d*")) { $userPath = ($userPath.TrimEnd(';') + ';' + $d); Write-Host "PATH += $d" }
}
[Environment]::SetEnvironmentVariable('Path', $userPath, 'User')
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + $userPath

$vpy = Join-Path $Runtime 'Scripts\python.exe'
if (-not (Test-Path $vpy)) { Write-Host "Runtime not found at $Runtime - run setup\rebuild_runtime.ps1 first"; Stop-Transcript | Out-Null; exit 1 }
& $vpy -m pip install --quiet ocrmypdf pymupdf markitdown extract-msg chardet pywin32

Write-Host '== checks'
& $vpy -c "import ocrmypdf, fitz, markitdown, extract_msg, chardet, win32com.client; print('python packages OK')"
tesseract --version | Select-Object -First 1
& $vpy -m ocrmypdf --version
if ($gs) { & (Join-Path $gs.FullName 'bin\gswin64c.exe') --version }
ebook-convert --version | Select-Object -First 1
$word = $null; try { $word = New-Object -ComObject Word.Application; $word.Quit(); Write-Host 'Word (COM) available: .doc files convert without LibreOffice' } catch { Write-Host 'Word (COM) NOT available: install LibreOffice for .doc files (winget install -e --id TheDocumentFoundation.LibreOffice)' }
Stop-Transcript | Out-Null
Write-Host "Log: $log"
