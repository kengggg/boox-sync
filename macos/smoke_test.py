#!/usr/bin/env python3
"""Validate a built app using synthetic data, without contacting any tablet."""

import argparse
import json
import os
import plistlib
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader

from boox_sync.note import build_note


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    args = parser.parse_args()
    app = args.app.resolve()
    helper = app / "Contents/Helpers/BOOXEngine.app/Contents/MacOS/boox-engine"
    adb = app / "Contents/Helpers/adb"
    environment = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        check=True,
    )
    subprocess.run([str(adb), "version"], env=environment, check=True, timeout=15)
    with tempfile.TemporaryDirectory(prefix="boox-package-smoke-") as temporary:
        root = Path(temporary)

        def call(command, **extra):
            result = subprocess.run(
                [str(helper)],
                input=json.dumps({"protocol": 1, "command": command, **extra}) + "\n",
                text=True,
                capture_output=True,
                cwd=root,
                env=environment,
                check=True,
                timeout=30,
            )
            events = [json.loads(line) for line in result.stdout.splitlines()]
            assert events[-1]["protocol"] == 1
            assert events[-1]["event"] == "result" and events[-1]["ok"]
            return events[-1]["data"]

        health = call("health")
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        assert health["version"] == info["CFBundleShortVersionString"]
        source, target = root / "Thai sample.note", root / "Thai sample.pdf"
        source.write_bytes(
            build_note(
                note_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                title="Packaged Thai sample",
                strokes=[
                    {
                        "pen_type": 6,
                        "text": "Hello สวัสดี ภาษาไทย 123\nน้ำ กิ กี่ กี้ กิ๊ กิ๋",
                        "style": {"textSize": 20},
                        "bbox": {"left": 10, "top": 20, "right": 480},
                    }
                ],
            )
        )
        result = call("render_file", path=str(source), output=str(target))
        assert result["status"] == "ok", result
        reader = PdfReader(target)
        assert len(reader.pages) == 1
        text = reader.pages[0].extract_text()
        assert "สวัสดี" in text and "■" not in text
        assert b"/ActualText" in reader.pages[0].get_contents().get_data()
    print("PASS: signatures, bundled ADB, protocol, and mixed Thai/Latin PDF")


if __name__ == "__main__":
    main()
