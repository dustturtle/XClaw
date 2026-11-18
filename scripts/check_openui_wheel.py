"""Check that the wheel contains the exact assets produced by the frontend build."""
import sys
from pathlib import Path
from zipfile import ZipFile


def main(directory):
    wheels = list(directory.glob("xclaw-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"Expected one XClaw wheel in {directory}, found {len(wheels)}")
    assets = Path("xclaw/channels/static")
    with ZipFile(wheels[0]) as wheel:
        for name in ("openui.js", "openui.css", "openui-host.js", "openui-prompt.txt", "openui-licenses.txt"):
            path = assets / name
            if not path.stat().st_size or wheel.read(path.as_posix()) != path.read_bytes():
                raise SystemExit(f"Missing or stale wheel asset: {name}")
    print(f"Verified OpenUI assets in {wheels[0].name}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
