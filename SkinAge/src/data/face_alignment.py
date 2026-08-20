"""
MediaPipe-based face detection and affine alignment pipeline.

Detects faces, extracts 468-point landmarks via Face Mesh, and produces
geometrically normalised (eyes-horizontal, fixed inter-eye distance) crops
suitable for downstream skin-age estimation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import mediapipe as mp
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # SkinAge/

# ---------------------------------------------------------------------------
# Landmark index groups for eye centres
# ---------------------------------------------------------------------------
LEFT_EYE_INDICES: List[int] = [33, 133, 157, 158, 159, 160, 161, 246]
RIGHT_EYE_INDICES: List[int] = [362, 263, 384, 385, 386, 387, 388, 466]

# Target inter-eye distance (pixels) at 512x512 output
TARGET_INTER_EYE_DISTANCE: float = 180.0
DEFAULT_OUTPUT_SIZE: int = 512

# MediaPipe confidence threshold
DETECTION_CONFIDENCE_THRESHOLD: float = 0.7


# ---------------------------------------------------------------------------
# Return-type dataclasses
# ---------------------------------------------------------------------------
@dataclass
class FaceDetection:
    """Result of a single face detection."""

    xmin: int
    ymin: int
    width: int
    height: int
    confidence: float

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass
class AlignmentResult:
    """Full output of the alignment pipeline for one image."""

    aligned_image: np.ndarray
    landmarks: np.ndarray  # (468, 2) pixel coordinates on the *original* image
    transform_matrix: np.ndarray  # 2x3 affine matrix
    face_bbox: FaceDetection
    confidence: float


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

import urllib.request

_TASK_MODEL_PATH = _PROJECT_ROOT / "outputs" / "models" / "mediapipe" / "face_landmarker.task"
_TASK_MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task"

# Additional read-only search locations, tried before attempting a download.
# In the Docker image the models are baked into /opt/mediapipe-models because
# outputs/models is bind-mounted read-only in production, which makes both the
# in-place download and even mkdir impossible.
_TASK_MODEL_SEARCH_PATHS = (
    _TASK_MODEL_PATH,
    Path("/opt/mediapipe-models/face_landmarker.task"),
)


def _ensure_task_model() -> str:
    """Return a path to the MediaPipe Tasks face landmarker model.

    Checks the known locations first and only falls back to downloading when
    none of them contain the model. The download is best-effort: on a
    read-only filesystem it fails and we return the default path so the caller
    can surface a clear initialisation error.
    """
    for candidate in _TASK_MODEL_SEARCH_PATHS:
        if candidate.is_file():
            return str(candidate)

    try:
        _TASK_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Downloading face_landmarker.task from Google Storage...")
        urllib.request.urlretrieve(_TASK_MODEL_URL, str(_TASK_MODEL_PATH))
        logger.info("face_landmarker.task successfully downloaded.")
    except Exception as exc:
        logger.warning("Could not auto-download face_landmarker.task: %s", exc)

    return str(_TASK_MODEL_PATH)


def _get_face_detection_class():
    try:
        from mediapipe.python.solutions import face_detection
        return face_detection.FaceDetection
    except Exception:
        pass
    try:
        import mediapipe as mp
        if hasattr(mp, "solutions") and hasattr(mp.solutions, "face_detection"):
            return mp.solutions.face_detection.FaceDetection
    except Exception:
        pass
    try:
        import mediapipe.python.solutions.face_detection as fd
        return fd.FaceDetection
    except Exception:
        pass
    return None


def _get_face_mesh_class():
    try:
        from mediapipe.python.solutions import face_mesh
        return face_mesh.FaceMesh
    except Exception:
        pass
    try:
        import mediapipe as mp
        if hasattr(mp, "solutions") and hasattr(mp.solutions, "face_mesh"):
            return mp.solutions.face_mesh.FaceMesh
    except Exception:
        pass
    try:
        import mediapipe.python.solutions.face_mesh as fm
        return fm.FaceMesh
    except Exception:
        pass
    return None


def detect_face(image: np.ndarray) -> Optional[FaceDetection]:
    """Detect the primary face in an image using MediaPipe Face Detection.

    When multiple faces are found the one with the largest bounding-box area
    is returned. Returns None when no face meets the threshold.
    """
    if image is None or image.size == 0:
        logger.warning("detect_face received an empty image.")
        return None

    h, w = image.shape[:2]
    FaceDetectionClass = _get_face_detection_class()

    if FaceDetectionClass is not None:
        try:
            with FaceDetectionClass(
                model_selection=1,  # full-range model
                min_detection_confidence=DETECTION_CONFIDENCE_THRESHOLD,
            ) as face_detection:
                rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                results = face_detection.process(rgb)

                if results.detections:
                    best: Optional[FaceDetection] = None
                    for det in results.detections:
                        bbox_rel = det.location_data.relative_bounding_box
                        xmin = max(int(bbox_rel.xmin * w), 0)
                        ymin = max(int(bbox_rel.ymin * h), 0)
                        box_w = min(int(bbox_rel.width * w), w - xmin)
                        box_h = min(int(bbox_rel.height * h), h - ymin)
                        conf = det.score[0]

                        candidate = FaceDetection(
                            xmin=xmin, ymin=ymin, width=box_w, height=box_h, confidence=conf
                        )
                        if best is None or candidate.area > best.area:
                            best = candidate
                    return best
        except Exception as exc:
            logger.debug("FaceDetection failed: %s", exc)

    # Fallback to landmarks bounding box
    lms = get_landmarks(image)
    if lms is not None:
        min_x = max(0, int(lms[:, 0].min()))
        min_y = max(0, int(lms[:, 1].min()))
        max_x = min(w, int(lms[:, 0].max()))
        max_y = min(h, int(lms[:, 1].max()))
        return FaceDetection(
            xmin=min_x, ymin=min_y, width=max(1, max_x - min_x), height=max(1, max_y - min_y), confidence=0.9
        )

    return None


def decode_image_bytes(image_bytes: bytes) -> Optional[np.ndarray]:
    """Decode raw image bytes with EXIF orientation correction, returning BGR array."""
    import io
    from PIL import Image, ImageOps

    try:
        pil_img = Image.open(io.BytesIO(image_bytes))
        pil_img = ImageOps.exif_transpose(pil_img)
        if pil_img.mode != "RGB":
            pil_img = pil_img.convert("RGB")
        return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    except Exception:
        nparr = np.frombuffer(image_bytes, np.uint8)
        return cv2.imdecode(nparr, cv2.IMREAD_COLOR)


import threading

_FACE_MESH_SINGLETON = None
_TASK_LANDMARKER_SINGLETON = None
_FACE_MESH_LOCK = threading.Lock()


def _get_face_mesh():
    """Return a process-wide singleton FaceMesh instance (Solutions API)."""
    global _FACE_MESH_SINGLETON
    if _FACE_MESH_SINGLETON is None:
        with _FACE_MESH_LOCK:
            if _FACE_MESH_SINGLETON is None:
                FaceMeshClass = _get_face_mesh_class()
                if FaceMeshClass is not None:
                    try:
                        _FACE_MESH_SINGLETON = FaceMeshClass(
                            static_image_mode=True,
                            max_num_faces=1,
                            refine_landmarks=True,
                            min_detection_confidence=0.5,
                        )
                    except Exception as exc:
                        logger.debug("Could not initialize FaceMesh solutions: %s", exc)
                        _FACE_MESH_SINGLETON = False
                else:
                    _FACE_MESH_SINGLETON = False
    return _FACE_MESH_SINGLETON if _FACE_MESH_SINGLETON is not False else None


def _get_tasks_landmarker():
    """Return a process-wide singleton Tasks FaceLandmarker instance."""
    global _TASK_LANDMARKER_SINGLETON
    if _TASK_LANDMARKER_SINGLETON is None:
        with _FACE_MESH_LOCK:
            if _TASK_LANDMARKER_SINGLETON is None:
                try:
                    from mediapipe.tasks.python import BaseOptions
                    from mediapipe.tasks.python.vision import FaceLandmarker, FaceLandmarkerOptions
                    model_path = _ensure_task_model()
                    options = FaceLandmarkerOptions(
                        base_options=BaseOptions(model_asset_path=model_path),
                        num_faces=1,
                        min_face_detection_confidence=0.5,
                        min_face_presence_confidence=0.5,
                    )
                    _TASK_LANDMARKER_SINGLETON = FaceLandmarker.create_from_options(options)
                    logger.info("Tasks FaceLandmarker initialised from %s", model_path)
                except Exception as exc:
                    # Logged at WARNING (not DEBUG): if this fails and the
                    # legacy Solutions API is also unavailable, every request
                    # fails its quality checks with no other clue as to why.
                    logger.warning(
                        "Could not initialize Tasks FaceLandmarker: %s", exc, exc_info=True
                    )
                    _TASK_LANDMARKER_SINGLETON = False
    return _TASK_LANDMARKER_SINGLETON if _TASK_LANDMARKER_SINGLETON is not False else None


def get_landmarks(image: np.ndarray) -> Optional[np.ndarray]:
    """Extract 468 face-mesh landmarks and return pixel coordinates.

    Supports both legacy Solutions FaceMesh and modern MediaPipe Tasks FaceLandmarker.
    Returns an array of shape ``(468, 2)`` with (x, y) in pixel space, or ``None``.
    """
    if image is None or image.size == 0:
        logger.warning("get_landmarks received an empty image.")
        return None

    h, w = image.shape[:2]
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

    # 1. Try Solutions FaceMesh
    face_mesh = _get_face_mesh()
    if face_mesh is not None:
        try:
            with _FACE_MESH_LOCK:
                results = face_mesh.process(rgb)
            if results.multi_face_landmarks:
                face = results.multi_face_landmarks[0]
                num_pts = min(len(face.landmark), 468)
                return np.array(
                    [[face.landmark[i].x * w, face.landmark[i].y * h] for i in range(num_pts)],
                    dtype=np.float32,
                )
        except Exception as exc:
            logger.debug("Solutions FaceMesh processing error: %s", exc)

    # 2. Try Tasks FaceLandmarker
    task_landmarker = _get_tasks_landmarker()
    if task_landmarker is not None:
        try:
            import mediapipe as mp
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            with _FACE_MESH_LOCK:
                res = task_landmarker.detect(mp_img)
            if res.face_landmarks:
                face = res.face_landmarks[0]
                num_pts = min(len(face), 468)
                return np.array(
                    [[face[i].x * w, face[i].y * h] for i in range(num_pts)],
                    dtype=np.float32,
                )
        except Exception as exc:
            logger.debug("Tasks FaceLandmarker processing error: %s", exc)

    return None


def get_landmarks_robust(image: np.ndarray) -> Tuple[np.ndarray, Optional[np.ndarray]]:
    """Extract landmarks, automatically rotating image by 0/90/180/270 deg if needed.

    Returns (oriented_image, landmarks).
    """
    if image is None or image.size == 0:
        return image, None

    # Try 0 deg
    lms = get_landmarks(image)
    if lms is not None:
        return image, lms

    # Try 180 deg (common for upside-down selfies)
    img_180 = cv2.rotate(image, cv2.ROTATE_180)
    lms_180 = get_landmarks(img_180)
    if lms_180 is not None:
        logger.info("Face detected after 180 deg rotation.")
        return img_180, lms_180

    # Try 90 deg clockwise
    img_90 = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    lms_90 = get_landmarks(img_90)
    if lms_90 is not None:
        logger.info("Face detected after 90 deg rotation.")
        return img_90, lms_90

    # Try 270 deg clockwise
    img_270 = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    lms_270 = get_landmarks(img_270)
    if lms_270 is not None:
        logger.info("Face detected after 270 deg rotation.")
        return img_270, lms_270

    return image, None


def _eye_centres(landmarks: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Compute left and right eye centres from landmark indices."""
    left_eye = landmarks[LEFT_EYE_INDICES].mean(axis=0)
    right_eye = landmarks[RIGHT_EYE_INDICES].mean(axis=0)
    return left_eye, right_eye


def align_face(
    image: np.ndarray,
    landmarks: np.ndarray,
    output_size: int = DEFAULT_OUTPUT_SIZE,
) -> Tuple[np.ndarray, np.ndarray]:
    """Compute and apply an affine warp that normalises head pose.

    The transform:
      1. Rotates so the line between the eyes is horizontal.
      2. Scales so the inter-eye distance equals
         ``TARGET_INTER_EYE_DISTANCE`` (180 px at 512x512), proportionally
         scaled for other output sizes.
      3. Translates so the midpoint between the eyes sits at the centre of
         the output image.

    Parameters
    ----------
    image : np.ndarray
        BGR image (uint8).
    landmarks : np.ndarray
        (468, 2) array of pixel-coordinate landmarks.
    output_size : int
        Width and height of the square output crop.

    Returns
    -------
    aligned_face : np.ndarray
        The warped output image (``output_size x output_size``, BGR, uint8).
    transform_matrix : np.ndarray
        The 2x3 affine matrix applied by ``cv2.warpAffine``.
    """
    left_eye, right_eye = _eye_centres(landmarks)

    # --- rotation angle ---
    delta = right_eye - left_eye
    angle_rad = np.arctan2(delta[1], delta[0])
    angle_deg = float(np.degrees(angle_rad))

    # --- scale ---
    current_dist = float(np.linalg.norm(delta))
    if current_dist < 1e-6:
        logger.warning("Degenerate eye distance; returning unaligned crop.")
        current_dist = 1.0

    # Scale target proportionally if output_size differs from 512
    scaled_target = TARGET_INTER_EYE_DISTANCE * (output_size / DEFAULT_OUTPUT_SIZE)
    scale = scaled_target / current_dist

    # --- centre of the face (midpoint between eyes) ---
    face_centre = (left_eye + right_eye) / 2.0

    # Build the affine: rotate+scale around the face centre, then translate
    # so the face centre lands at the output image centre.
    rotation_matrix = cv2.getRotationMatrix2D(
        center=(float(face_centre[0]), float(face_centre[1])),
        angle=angle_deg,
        scale=scale,
    )  # 2x3

    # Adjust translation so face centre maps to output centre
    output_centre = np.array([output_size / 2.0, output_size / 2.0])
    rotation_matrix[0, 2] += output_centre[0] - face_centre[0]
    rotation_matrix[1, 2] += output_centre[1] - face_centre[1]

    aligned_face = cv2.warpAffine(
        image,
        rotation_matrix,
        (output_size, output_size),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT_101,
    )

    return aligned_face, rotation_matrix.astype(np.float64)


# ---------------------------------------------------------------------------
# High-level pipelines
# ---------------------------------------------------------------------------

_SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}


def process_image(
    image_path: str,
    output_size: int = DEFAULT_OUTPUT_SIZE,
) -> Optional[AlignmentResult]:
    """Full pipeline: load -> detect -> landmarks -> align.

    Returns ``None`` if any stage fails (image unreadable, no face, etc.).
    """
    path = Path(image_path)
    if not path.is_file():
        logger.error("Image file not found: %s", image_path)
        return None

    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        logger.error("Failed to decode image: %s", image_path)
        return None

    detection = detect_face(image)
    if detection is None:
        logger.info("No face detected in %s", image_path)
        return None

    landmarks = get_landmarks(image)
    if landmarks is None:
        logger.info("No face mesh landmarks in %s", image_path)
        return None

    aligned_image, transform_matrix = align_face(image, landmarks, output_size)

    return AlignmentResult(
        aligned_image=aligned_image,
        landmarks=landmarks,
        transform_matrix=transform_matrix,
        face_bbox=detection,
        confidence=detection.confidence,
    )


def batch_process(
    input_dir: str,
    output_dir: str,
    output_size: int = DEFAULT_OUTPUT_SIZE,
) -> pd.DataFrame:
    """Process every supported image in *input_dir* and write results.

    For each successfully aligned image two files are written to
    *output_dir*:
      - ``<stem>_aligned.png``  -- the aligned face crop
      - ``<stem>_landmarks.json`` -- the 468 landmark coordinates

    Returns a :class:`~pandas.DataFrame` with columns:
        original_path, aligned_path, landmarks_path, confidence, success
    """
    in_path = Path(input_dir)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    records: List[dict] = []

    image_files = sorted(
        p for p in in_path.iterdir()
        if p.suffix.lower() in _SUPPORTED_EXTENSIONS
    )

    if not image_files:
        logger.warning("No supported images found in %s", input_dir)

    for img_file in image_files:
        record: dict = {
            "original_path": str(img_file),
            "aligned_path": "",
            "landmarks_path": "",
            "confidence": 0.0,
            "success": False,
        }

        try:
            result = process_image(str(img_file), output_size)

            if result is None:
                logger.info("Skipping %s (alignment failed).", img_file.name)
                records.append(record)
                continue

            stem = img_file.stem

            # Save aligned image
            aligned_file = out_path / f"{stem}_aligned.png"
            cv2.imwrite(str(aligned_file), result.aligned_image)

            # Save landmarks as JSON
            landmarks_file = out_path / f"{stem}_landmarks.json"
            landmarks_data = {
                "landmarks": result.landmarks.tolist(),
                "transform_matrix": result.transform_matrix.tolist(),
                "face_bbox": {
                    "xmin": result.face_bbox.xmin,
                    "ymin": result.face_bbox.ymin,
                    "width": result.face_bbox.width,
                    "height": result.face_bbox.height,
                    "confidence": result.face_bbox.confidence,
                },
            }
            with open(landmarks_file, "w", encoding="utf-8") as fh:
                json.dump(landmarks_data, fh, indent=2)

            record.update(
                {
                    "aligned_path": str(aligned_file),
                    "landmarks_path": str(landmarks_file),
                    "confidence": result.confidence,
                    "success": True,
                }
            )
            logger.info("Aligned %s (confidence=%.3f).", img_file.name, result.confidence)

        except Exception:
            logger.exception("Unexpected error processing %s.", img_file.name)

        records.append(record)

    df = pd.DataFrame(records)
    return df
