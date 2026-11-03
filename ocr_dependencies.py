"""빌드에 실제 필요한 Tesseract DLL만 찾는다."""

from collections import deque
from pathlib import Path
import sys

import pefile


def find_dependencies(directory: Path) -> list[Path]:
    executable = directory / "tesseract.exe"
    if not executable.is_file():
        raise FileNotFoundError(executable)

    local_files = {path.name.casefold(): path for path in directory.glob("*.dll")}
    local_files[executable.name.casefold()] = executable
    pending = deque([executable.name.casefold()])
    visited = set()

    while pending:
        name = pending.popleft()
        if name in visited:
            continue
        visited.add(name)
        binary = pefile.PE(str(local_files[name]), fast_load=True)
        try:
            binary.parse_data_directories(directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            ])
            for kind in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
                for dependency in getattr(binary, kind, []):
                    dependency_name = dependency.dll.decode(errors="replace").casefold()
                    if dependency_name in local_files:
                        pending.append(dependency_name)
        finally:
            binary.close()

    return sorted((local_files[name] for name in visited), key=lambda path: path.name.casefold())


if __name__ == "__main__":
    for path in find_dependencies(Path(sys.argv[1])):
        print(path)