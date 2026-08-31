#!/usr/bin/env python3
"""Run a repeatable face-detection test on still images.

This host-side harness validates the image/result contract before a camera is
available.  The detector is intentionally replaceable: the default OpenCV
Haar detector is only a smoke-test backend; firmware will use ESP-DL with the
same FaceDetectionResult fields.
"""

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect faces in test images")
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, help="Write JSON results here")
    parser.add_argument("--annotated-dir", type=Path, help="Write images with boxes")
    parser.add_argument("--scale-factor", type=float, default=1.1)
    parser.add_argument("--min-neighbors", type=int, default=5)
    args = parser.parse_args()

    try:
        import cv2
    except ImportError:
        print("OpenCV is required: python -m pip install -r scripts/requirements-face-test.txt", file=sys.stderr)
        return 2

    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(str(cascade_path))
    if detector.empty():
        print(f"Failed to load detector: {cascade_path}", file=sys.stderr)
        return 2

    results = []
    for image_path in args.images:
        image = cv2.imread(str(image_path))
        if image is None:
            print(f"Unable to read image: {image_path}", file=sys.stderr)
            return 2
        height, width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        boxes = detector.detectMultiScale(gray, args.scale_factor, args.min_neighbors)
        faces = []
        for x, y, w, h in boxes:
            faces.append({
                "x": int(x), "y": int(y), "width": int(w), "height": int(h),
                "center_x": round((x + w / 2) / width, 4),
                "center_y": round((y + h / 2) / height, 4),
            })
            if args.annotated_dir:
                cv2.rectangle(image, (x, y), (x + w, y + h), (0, 220, 0), 2)
        if args.annotated_dir:
            args.annotated_dir.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(args.annotated_dir / image_path.name), image)
        results.append({"image": str(image_path), "width": width, "height": height,
                        "face_count": len(faces), "faces": faces})

    payload = json.dumps(results, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
