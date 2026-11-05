# 문서 OCR 정리

Windows 폴더의 문서를 읽고 비슷한 파일을 폴더별로 정리합니다. PDF·사진은 OCR로 읽고, 중복 파일도 확인합니다.

## 다운로드

[Windows 앱 다운로드](https://github.com/vmvvmvvmv/organizing_folder_auto/releases/latest)

릴리스의 실행 파일을 받아 실행하세요. 로컬 AI 분류는 현재 소스 코드에만 반영되어 있습니다.

## 사용 방법

![문서 OCR 정리 화면](assets/app-screen.png)

1. 원본 폴더와 결과 폴더를 선택합니다.
2. 분류 미리보기에서 파일을 폴더로 끌어 놓거나, 이름을 더블클릭·우클릭해 수정합니다.
3. 정리 시작을 누르면 파일이 결과 폴더로 이동합니다. 처리 결과는 CSV 파일로 저장됩니다.

## 로컬 AI 분류 (선택)

[Ollama](https://ollama.com/download/windows)를 설치하고 아래 모델을 받으세요.

```powershell
ollama pull qwen3-embedding:0.6b
ollama pull qwen3:4b-instruct
```

**로컬 AI 분류 (규칙 검증 포함)**를 켜면 공통 단어 규칙 대신 로컬 AI가 폴더를 제안합니다. AI가 제안한 이름은 다음 규칙으로 확인·보정합니다.

- 파일명이나 본문에 근거가 없는 폴더명은 받지 않고 미분류로 둡니다.
- GIS·프로그램 구성 파일은 같은 폴더의 세트를 한 폴더로 맞추고, 주제 근거가 없으면 형식 폴더(예: GIS 자료)에 둡니다.
- 파일이 1~2개뿐인 폴더는 내용이 매우 비슷한 큰 폴더로 합칩니다. 분류 이유에 원래 폴더명이 남습니다.

AI가 판단하지 못한 파일은 미분류로 남습니다. 모델은 앱에 포함되지 않습니다.

라이선스: [AGPL-3.0](LICENSE)
