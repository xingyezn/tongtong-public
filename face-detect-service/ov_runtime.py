"""Self-contained OpenVINO inference path for the exported face detector.

Vendored from https://github.com/rubythalib-ai/face-detection-openvino-edge
(src/ov_runtime.py, MIT license). Deliberately has no Ultralytics/PyTorch
dependency so it stays light on a 2-core / 2 GB deployment box.
"""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
import openvino as ov

DEFAULT_THREADS = int(os.getenv("OV_NUM_THREADS", "2"))


def cpu_config(num_threads=DEFAULT_THREADS, num_streams=1, hint="LATENCY"):
    return {
        "INFERENCE_NUM_THREADS": num_threads,
        "NUM_STREAMS": num_streams,
        "PERFORMANCE_HINT": hint,
        "ENABLE_CPU_PINNING": "YES",
    }


def letterbox(img, new_shape=640, color=(114, 114, 114)):
    """Resize preserving aspect ratio and pad. Returns (image, scale, (pad_x, pad_y))."""
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)
    h, w = img.shape[:2]
    r = min(new_shape[0] / h, new_shape[1] / w)
    nh, nw = int(round(h * r)), int(round(w * r))
    if (nh, nw) != (h, w):
        interp = cv2.INTER_AREA if r < 1 else cv2.INTER_LINEAR
        img = cv2.resize(img, (nw, nh), interpolation=interp)
    dh, dw = new_shape[0] - nh, new_shape[1] - nw
    top, left = dh // 2, dw // 2
    img = cv2.copyMakeBorder(img, top, dh - top, left, dw - left, cv2.BORDER_CONSTANT, value=color)
    return img, r, (left, top)


def nms(boxes, scores, iou_thres):
    """Plain greedy NMS on xyxy boxes."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1).clip(0) * (y2 - y1).clip(0)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = (xx2 - xx1).clip(0) * (yy2 - yy1).clip(0)
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_thres]
    return keep


class FaceDetector:
    """OpenVINO face detector: preprocess -> infer -> decode -> NMS."""

    def __init__(self, model_dir, device="CPU", imgsz=640, num_threads=DEFAULT_THREADS,
                 conf=0.25, iou=0.45):
        self.imgsz = imgsz
        self.conf = conf
        self.iou = iou

        model_dir = Path(model_dir)
        xml = model_dir if model_dir.suffix == ".xml" else next(model_dir.glob("*.xml"))

        self.core = ov.Core()
        model = self.core.read_model(xml)
        cfg = cpu_config(num_threads) if device == "CPU" else {}
        self.compiled = self.core.compile_model(model, device, cfg)
        self.request = self.compiled.create_infer_request()
        self.input_port = self.compiled.input(0)
        self.output_port = self.compiled.output(0)

        shape = self.input_port.get_partial_shape()
        if shape[2].is_static and shape[3].is_static:
            self.imgsz = (shape[2].get_length(), shape[3].get_length())
        else:
            self.imgsz = (imgsz, imgsz)

    def preprocess(self, img):
        padded, r, pad = letterbox(img, self.imgsz)
        blob = padded[:, :, ::-1].transpose(2, 0, 1)  # BGR->RGB, HWC->CHW
        blob = np.ascontiguousarray(blob, dtype=np.float32) / 255.0
        return blob[None], r, pad

    def infer(self, blob):
        self.request.infer({self.input_port: blob})
        return self.request.get_output_tensor(0).data

    def postprocess(self, raw, r, pad, orig_shape):
        pred = raw[0]
        if pred.shape[0] < pred.shape[1]:
            pred = pred.T

        scores = pred[:, 4:].max(axis=1)
        keep = scores > self.conf
        if not keep.any():
            return np.zeros((0, 4), np.float32), np.zeros((0,), np.float32)
        pred, scores = pred[keep], scores[keep]

        cx, cy, w, h = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
        boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)

        boxes[:, [0, 2]] -= pad[0]
        boxes[:, [1, 3]] -= pad[1]
        boxes /= r
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, orig_shape[1])
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, orig_shape[0])

        idx = nms(boxes, scores, self.iou)
        return boxes[idx], scores[idx]

    def __call__(self, img):
        blob, r, pad = self.preprocess(img)
        raw = self.infer(blob)
        return self.postprocess(raw, r, pad, img.shape[:2])
