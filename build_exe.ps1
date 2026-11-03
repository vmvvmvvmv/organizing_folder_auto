param(
    [string]$TesseractDir,
    [string]$Name = 'DocumentOrganizer',
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = if ($PythonPath) { $PythonPath } else { Join-Path $root '.venv\Scripts\python.exe' }
$model = Join-Path $root 'models\kor.traineddata'
$rules = Join-Path $root 'config\classification.json'
$license = Join-Path $root 'LICENSE'
$notice = Join-Path $root 'THIRD_PARTY_NOTICES.txt'
$apache = Join-Path $root 'licenses\Apache-2.0.txt'

if ($Name -notmatch '^[A-Za-z0-9_-]+$') { throw '실행 파일 이름에는 영문, 숫자, 밑줄, 하이픈만 사용할 수 있습니다.' }
if (-not (Test-Path -LiteralPath $python)) { throw '전용 Python 환경이 없습니다. README의 설치 단계를 확인하세요.' }
foreach ($file in @($model, $rules, $license, $notice, $apache)) {
    if (-not (Test-Path -LiteralPath $file)) { throw "빌드에 필요한 파일이 없습니다: $file" }
}

if (-not $TesseractDir) {
    $TesseractDir = @(
        (Join-Path $env:ProgramFiles 'Tesseract-OCR')
        (Join-Path $env:LOCALAPPDATA 'Programs\Tesseract-OCR')
        (Join-Path $env:LOCALAPPDATA 'Tesseract-OCR')
    ) | Where-Object { Test-Path -LiteralPath (Join-Path $_ 'tesseract.exe') } | Select-Object -First 1
}
if (-not $TesseractDir -or -not (Test-Path -LiteralPath (Join-Path $TesseractDir 'tesseract.exe'))) {
    throw 'Tesseract-OCR을 찾지 못했습니다. -TesseractDir로 설치 경로를 지정하세요.'
}

$pyInstallerArgs = @(
    '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
    '--name', $Name,
    '--distpath', (Join-Path $root 'dist'),
    '--workpath', (Join-Path $root 'build'),
    '--specpath', $root,
    '--hidden-import', 'pymupdf',
    '--add-data', ($model + ';models'),
    '--add-data', ($rules + ';config'),
    '--add-data', ($license + ';.'),
    '--add-data', ($notice + ';.'),
    '--add-data', ($apache + ';licenses')
)
$ocrFiles = @(& $python (Join-Path $root 'ocr_dependencies.py') $TesseractDir)
if ($LASTEXITCODE -ne 0 -or $ocrFiles.Count -eq 0) {
    throw 'Tesseract 의존성 확인에 실패했습니다.'
}
foreach ($file in $ocrFiles) {
    $pyInstallerArgs += @('--add-binary', "${file};ocr")
}
$pyInstallerArgs += (Join-Path $root 'app.py')

& $python @pyInstallerArgs
if ($LASTEXITCODE -ne 0) { throw '실행 파일 빌드에 실패했습니다.' }
Write-Host "완료: $(Join-Path (Join-Path $root 'dist') "$Name.exe")"