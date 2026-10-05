"""Build a portable Windows folder from this repository's source."""
import os
from pathlib import Path
import shutil


def main():
    if os.name != "nt":
        raise SystemExit("Please build SlotBot on Windows.")
    from PyInstaller.__main__ import run

    root = Path(__file__).resolve().parent
    run([
        str(root / "run_slotbot.py"),
        "--name", "SlotBot",
        "--onedir", "--windowed", "--noconfirm",
        "--manifest", str(root / "SlotBot.manifest"),
        "--paths", str(root),
        "--distpath", str(root / "dist"),
        "--workpath", str(root / "build"),
        "--specpath", str(root / "build"),
    ])
    output = root / "dist" / "SlotBot"
    # Runtime data is read beside the executable, outside PyInstaller's _internal.
    for name in ("数字截图", "templates"):
        shutil.copytree(root / name, output / name, dirs_exist_ok=True)
    for name in ("README.md", "LICENSE", "NOTICE.md", "config.example.json"):
        shutil.copy2(root / name, output / name)
    print(f"Build complete: {output / 'SlotBot.exe'}")
    print("Distribute the whole SlotBot folder, including _internal and the assets.")


if __name__ == "__main__":
    main()
