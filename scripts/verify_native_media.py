#!/usr/bin/env python3
"""Verify retained native Harness artifacts, images and complete MP4 decoding."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from PIL import Image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    run = args.run.resolve()
    source = run / "report.json"
    report = json.loads(source.read_text())
    if report["status"] != "passed":
        raise AssertionError("Native run did not pass")
    directory = Path(report["case_directory"]).resolve()
    images, videos = [], []
    for artifact in report["result"]["benchmark_artifacts"]:
        path = (directory / artifact["ref"]).resolve()
        if not path.is_relative_to(directory):
            raise AssertionError("Artifact escapes the case directory")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != artifact["sha256"] or path.stat().st_size != artifact["size_bytes"]:
            raise AssertionError("Artifact identity differs: " + artifact["ref"])
        identity = {"ref": artifact["ref"], "sha256": digest}
        if artifact["mime_type"].startswith("image/"):
            with Image.open(path) as picture:
                picture.verify()
            with Image.open(path) as picture:
                picture.load()
                images.append({**identity, "width": picture.width, "height": picture.height})
        elif artifact["mime_type"] == "video/mp4":
            probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=codec_name,width,height,nb_frames,duration", "-of", "json", str(path)],
                capture_output=True, text=True, check=True, timeout=30)
            streams = json.loads(probe.stdout)["streams"]
            if len(streams) != 1 or int(streams[0]["nb_frames"]) < 2000:
                raise AssertionError("Full native video is missing")
            subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0",
                "-f", "null", "-"], capture_output=True, text=True, check=True, timeout=120)
            videos.append({**identity, **streams[0], "full_decode": "passed"})
    if not images or not videos:
        raise AssertionError("Expected camera images and native video")
    result = {"status": "passed", "source_report_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "images_verified": len(images), "images": images, "videos": videos,
        "model_inference": report["model_inference"]}
    (run / "media-validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": "passed", "images_verified": len(images), "videos_verified": len(videos)}))


if __name__ == "__main__":
    main()
