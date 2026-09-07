import os
import sqlite3
import threading
import time

import cv2
import numpy as np


class FaceRecognitionStore:
    def __init__(self, db_path, detector_model, recognizer_model, threshold=0.363):
        self.db_path = db_path
        self.threshold = float(threshold)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("""CREATE TABLE IF NOT EXISTS faces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            external_id TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            embedding BLOB NOT NULL,
            image BLOB,
            image_type TEXT NOT NULL DEFAULT 'image/jpeg',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        self._db.commit()
        self.ready = False
        self.error = ""
        try:
            self._detector = cv2.FaceDetectorYN.create(detector_model, "", (320, 320), 0.8, 0.3, 5000)
            self._recognizer = cv2.FaceRecognizerSF.create(recognizer_model, "")
            self.ready = True
        except Exception as exc:
            self._detector = None
            self._recognizer = None
            self.error = str(exc)

    def _faces(self, image):
        h, w = image.shape[:2]
        self._detector.setInputSize((w, h))
        _, faces = self._detector.detect(image)
        return [] if faces is None else faces

    def _embedding(self, image):
        faces = self._faces(image)
        if len(faces) == 0:
            raise ValueError("录入失败：图片中未检测到人脸，请上传包含 1 张清晰人脸的图片。")
        if len(faces) > 1:
            raise ValueError("录入失败：图片中检测到 {} 张人脸，必须且只能包含 1 张人脸，请重新上传。".format(len(faces)))
        aligned = self._recognizer.alignCrop(image, faces[0])
        feature = self._recognizer.feature(aligned)
        return np.asarray(feature, dtype=np.float32).reshape(-1), faces[0]

    def _row(self, row):
        return {"id": row["id"], "name": row["name"], "external_id": row["external_id"], "note": row["note"],
                "created_at": row["created_at"], "updated_at": row["updated_at"], "has_image": bool(row["image"])}

    def list(self):
        with self._lock:
            return [self._row(row) for row in self._db.execute("SELECT * FROM faces ORDER BY id DESC")]

    def get(self, face_id):
        with self._lock:
            row = self._db.execute("SELECT * FROM faces WHERE id=?", (face_id,)).fetchone()
            return self._row(row) if row else None

    def image(self, face_id):
        with self._lock:
            row = self._db.execute("SELECT image, image_type FROM faces WHERE id=?", (face_id,)).fetchone()
            return (bytes(row["image"]), row["image_type"]) if row and row["image"] else None

    def create(self, name, external_id, note, image, image_data, image_type):
        if not self.ready:
            raise RuntimeError("recognition model not ready: " + self.error)
        embedding, _ = self._embedding(image)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cur = self._db.execute("INSERT INTO faces(name,external_id,note,embedding,image,image_type,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                                   (name, external_id, note, embedding.tobytes(), image_data, image_type, now, now))
            self._db.commit()
            return self.get(cur.lastrowid)

    def update(self, face_id, name, external_id, note, image=None, image_data=None, image_type=None):
        with self._lock:
            old = self._db.execute("SELECT * FROM faces WHERE id=?", (face_id,)).fetchone()
        if not old:
            return None
        embedding = None
        if image is not None:
            if not self.ready:
                raise RuntimeError("recognition model not ready: " + self.error)
            embedding, _ = self._embedding(image)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._db.execute("""UPDATE faces SET name=?, external_id=?, note=?, embedding=COALESCE(?,embedding),
                image=COALESCE(?,image), image_type=COALESCE(?,image_type), updated_at=? WHERE id=?""",
                             (name, external_id, note, embedding.tobytes() if embedding is not None else None, image_data, image_type, now, face_id))
            self._db.commit()
            return self.get(face_id)

    def delete(self, face_id):
        with self._lock:
            cur = self._db.execute("DELETE FROM faces WHERE id=?", (face_id,))
            self._db.commit()
            return cur.rowcount > 0

    def recognize(self, image):
        if not self.ready:
            raise RuntimeError("recognition model not ready: " + self.error)
        faces = self._faces(image)
        if len(faces) == 0:
            return {"summary": "共检测到 0 张人脸，识别出 0 张；图片中未检测到人脸。",
                    "count": 0, "detected_count": 0, "recognized_count": 0,
                    "faces": [], "threshold": self.threshold}
        with self._lock:
            rows = list(self._db.execute("SELECT * FROM faces"))
        if not rows:
            return {"summary": "共检测到 {} 张人脸，识别出 0 张；人脸库为空。".format(len(faces)),
                    "count": 0, "detected_count": len(faces),
                    "recognized_count": 0, "faces": [],
                    "detected_faces": [{"box": [round(float(v), 1) for v in face[:4]],
                                        "recognized": False, "name": "unknown"}
                                       for face in faces],
                    "threshold": self.threshold}
        results = []
        detected = []
        for face in faces:
            aligned = self._recognizer.alignCrop(image, face)
            query = np.asarray(self._recognizer.feature(aligned), dtype=np.float32).reshape(-1)
            best = None
            for row in rows:
                known = np.frombuffer(row["embedding"], dtype=np.float32)
                score = float(self._recognizer.match(query.reshape(1, -1), known.reshape(1, -1), cv2.FaceRecognizerSF_FR_COSINE))
                if best is None or score > best["score"]:
                    best = {"id": row["id"], "name": row["name"], "external_id": row["external_id"], "score": score,
                            "box": [round(float(v), 1) for v in face[:4]]}
            box = [round(float(v), 1) for v in face[:4]]
            if best:
                best["score"] = round(best["score"], 4)
            known = bool(best and best["score"] > self.threshold)
            detected_item = {"box": box, "recognized": known}
            if known:
                results.append(best)
                detected_item.update({"id": best["id"], "name": best["name"],
                                      "score": best["score"]})
            else:
                detected_item.update({"name": "unknown"})
            detected.append(detected_item)
        names = []
        for item in results:
            if item["name"] not in names:
                names.append(item["name"])
        known_names = "、".join(names) if names else "无"
        summary = "共检测到 {} 张人脸，识别出 {} 张；认识的人有：{}。".format(
            len(faces), len(results), known_names)
        if not results:
            summary += "未找到相似度大于 {:.2f} 的已登记人脸。".format(self.threshold)
        return {"summary": summary, "count": len(results),
                "detected_count": len(faces),
                "recognized_count": len(results), "faces": results,
                "detected_faces": detected,
                "threshold": self.threshold}
