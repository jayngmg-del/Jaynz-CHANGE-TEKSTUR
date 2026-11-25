"""Optional end-to-end test. Set JAYN_SAMPLE_BUNDLE to a user-provided UnityFS v6 sample."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
from app.main import scan_bundle, apply_resource_patch

sample_value = os.environ.get("JAYN_SAMPLE_BUNDLE")
if not sample_value:
    raise SystemExit("Set JAYN_SAMPLE_BUNDLE to a UnityFS v6 sample file to run the end-to-end test.")
sample = Path(sample_value)
summary, textures = scan_bundle(sample)
print("SCAN", summary)
target = next((x for x in textures if x["supported"]), None)
if not target:
    raise SystemExit("No supported external ASTC texture found in this sample.")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    image = Image.new("RGBA", (target["width"], target["height"]), (22, 18, 30, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 8, max(9, target["width"] - 8), max(9, target["height"] - 8)), fill=(135, 48, 185, 255))
    replacement = td / "replacement.png"
    image.save(replacement)
    output = td / (sample.stem + "-tested" + (sample.suffix or ".bundle"))
    report = apply_resource_patch(sample, output, target["id"], replacement, "JG_SMOKE_TEST")
    print("REPLACE", report)
    assert report["status"] == "PASS"
    assert report["output_bytes"] == report["original_bytes"]
    assert report["no_padding"] is True
