# 문서 OCR 정리

Windows 폴더의 파일을 읽고 비슷한 내용·파일명·형식별로 묶어 정리하는 로컬 프로그램입니다. PDF와 사진은 OCR로, DOCX·HWPX·XLSX·PPTX는 문서 본문으로 분류합니다. `.hwp`, GIS 자료, 압축 파일 등 본문을 읽지 않는 형식도 파일명과 확장자로 분류할 수 있습니다.

## 폴더 구성

```text
app.py                 화면과 미리보기
organizer.py           검사, 내용 검증 후 이동, 결과 기록
auto_groups.py         반복 단어로 폴더 제안
reader.py              문서 읽기와 OCR
classification_rules.py  분류 설정 읽기
batch.py               대량 처리용 명령줄 실행
config/classification.json  확장자별 폴더와 제외 단어
models/README.md       한국어 OCR 모델 준비 방법
tests/                 자동 테스트
```

## 준비와 실행

Python 3.11, Tesseract OCR이 필요합니다. 패키지와 한국어 OCR 모델은 2025년 8월까지 공개된 버전을 사용합니다. `models/README.md`에 따라 `kor.traineddata`를 준비하세요.

이 폴더에서 PowerShell을 열고 실행합니다.

```powershell
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe app.py
```

콘솔 창 없이 열려면 `start.cmd`를 더블클릭합니다. 문제가 생겼을 때는 위의 `python.exe app.py` 명령으로 오류를 확인할 수 있습니다.

1. 원본 폴더와 결과 폴더를 정합니다.
2. **분류 미리보기**에서 제안된 폴더를 확인하고 필요하면 이름을 바꿉니다.
3. **정리 시작**을 누르면 내용이 같은지 검증한 뒤 원본을 이동합니다. 중복 파일은 검증 후 정리합니다.

결과 폴더에 실행마다 `정리_결과_날짜_시간.csv`가 생성됩니다. 미리보기 후 원본 파일 목록이나 내용이 바뀌면 이동을 시작하지 않으므로 미리보기를 다시 실행하세요. 이동 도중 내용이 바뀐 파일과 중단·실패한 파일은 원본에 남습니다. GIS·프로그램 구성 파일은 관련 파일의 하위 폴더 구조를 유지하지만, 설치 파일이나 프로젝트의 내부 경로가 이동 후에도 동작하는지는 확인이 필요합니다.

## 분류 규칙 바꾸기

[`config/classification.json`](config/classification.json)의 `file_groups`에서 확장자별 폴더 이름과 원래 하위 폴더 구조를 유지할지(`preserve_structure`)를 정합니다. `ignored_words`는 폴더 이름 제안에서 제외할 단어입니다. JSON을 고친 뒤 앱을 다시 실행하세요. 실행 파일은 다시 빌드해야 변경된 규칙이 반영됩니다.

반복 단어 제안의 최소 횟수(파일 수에 따라 2·3·4회), 최대 제안 폴더 수(20개), PDF 앞부분 5쪽, 100MB 읽기 제한은 현재 코드의 기본값입니다. 이 숫자를 바꾸려면 [`auto_groups.py`](auto_groups.py)와 [`reader.py`](reader.py)를 수정해야 합니다.

## 테스트와 Windows 실행 파일

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
& .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_exe.ps1
```

빌드 결과는 `dist/DocumentOrganizer/DocumentOrganizer.exe`입니다. 실행 중인 EXE가 잠겨 다시 만들 수 없을 때는 `-Name DocumentOrganizer_New`를 지정하면 별도 이름으로 빌드할 수 있습니다. 다른 PC에는 `dist/DocumentOrganizer` 폴더 전체를 복사하세요.

Python 패키지는 `requirements.txt`와 `requirements-build.txt`에 버전이 고정돼 있습니다. Tesseract 실행 파일은 빌드하는 PC에 설치된 버전을 사용합니다.
