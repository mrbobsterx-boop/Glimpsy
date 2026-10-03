"""Для сборки на GitHub: номер сборки внутрь программы и файл version-<система>.json для автообновления.

  python scripts/release_info.py build <номер> <канал>      → glimpsy/_build.py
  python scripts/release_info.py manifest <номер> <система>  → dist/version-<система>.json
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    cmd, number = sys.argv[1], int(sys.argv[2])
    if cmd == "build":
        channel = sys.argv[3]
        (ROOT / "glimpsy" / "_build.py").write_text(
            f'"""Создаётся при сборке на GitHub."""\n\nBUILD = {number}\nCHANNEL = "{channel}"\n', encoding="utf-8")
        return
    sys.path.insert(0, str(ROOT))
    import glimpsy

    key = sys.argv[3]
    dist = ROOT / "dist"
    files = {p.name: p.stat().st_size for p in sorted(dist.iterdir())
             if p.is_file() and p.suffix in (".zip", ".exe", ".AppImage")}
    out = dist / f"version-{key}.json"
    out.write_text(json.dumps({"build": number, "version": glimpsy.__version__, "files": files},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
