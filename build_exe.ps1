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

if ($Name -notmatch '^[A-Za-z0-9_-]+$') { throw '실행 파일 이름에는 영문, 숫자, 밑줄, 하이픈만 사용할 수 있습니다.' }
if (-not (Test-Path -LiteralPath $python)) { throw '전용 Python 환경이 없습니다. README의 설치 단계를 확인하세요.' }
if (-not (Test-Path -LiteralPath $model)) { throw '한국어 OCR 모델이 없습니다. models/README.md를 확인하세요.' }
if (-not (Test-Path -LiteralPath $rules)) { throw '분류 설정 파일이 없습니다: config/classification.json' }

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

& $python -m PyInstaller --noconfirm --clean --onedir --windowed `
    --name $Name `
    --distpath (Join-Path $root 'dist') `
    --workpath (Join-Path $root 'build') `
    --specpath $root `
    --hidden-import pymupdf `
    --add-data "${model};models" `
    --add-data "${rules};config" `
    (Join-Path $root 'app.py')
if ($LASTEXITCODE -ne 0) { throw '실행 파일 빌드에 실패했습니다.' }

$output = Join-Path (Join-Path $root 'dist') $Name
$ocr = Join-Path $output 'ocr'
New-Item -ItemType Directory -Path $ocr -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $TesseractDir 'tesseract.exe') -Destination $ocr
Get-ChildItem -LiteralPath $TesseractDir -Filter '*.dll' -File | Copy-Item -Destination $ocr
Write-Host "완료: $(Join-Path $output "$Name.exe")"
