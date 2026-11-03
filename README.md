# 문서 OCR 정리

Windows 폴더의 문서를 읽고 비슷한 파일을 폴더별로 정리합니다. PDF·사진은 OCR로 읽고, 중복 파일도 확인합니다.

## 다운로드

[Windows 앱 다운로드](https://github.com/vmvvmvvmv/organizing_folder_auto/releases/latest)

릴리스의 실행 파일을 받아 실행하세요. 로컬 AI 분류는 현재 소스 코드에만 반영되어 있습니다.

## 사용 방법

![문서 OCR 정리 화면](assets/app-screen.png)

1. 원본 폴더와 결과 폴더를 선택합니다.
2. 분류 미리보기를 눌러 폴더와 파일을 확인합니다. 필요한 경우 폴더 이름을 바꿉니다.
3. 정리 시작을 누르면 파일이 결과 폴더로 이동합니다. 처리 결과는 CSV 파일로 저장됩니다.

## 로컬 AI 분류 (선택)

[Ollama](https://ollama.com/download/windows)를 설치하고 PowerShell에서 `ollama pull qwen3-embedding:0.6b`를 실행하세요. 앱에서 **로컬 AI 분류**를 켜고 분류 미리보기를 누르면, 읽을 수 있는 미분류 파일을 비슷한 기존 폴더에 제안합니다.

라이선스: [AGPL-3.0](LICENSE)
