"""
Image quality gating for the SkinAge ML pipeline.

Validates that input images meet minimum quality standards before inference.
Every check returns specific, actionable guidance so the end user knows
exactly how to fix the problem. All checks run unconditionally so the user
can fix everything in one go.

Uses standard MediaPipe FaceMesh solutions (no external .tflite files required)
with fallback to multi-angle rotation robustness.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # SkinAge/
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config" / "api_config.yaml"

# ---------------------------------------------------------------------------
# MediaPipe landmark indices used for geometric checks
# ---------------------------------------------------------------------------
_LEFT_CHEEK_IDX = 234
_RIGHT_CHEEK_IDX = 454
_FOREHEAD_IDX = 10
_CHIN_IDX = 152
_NOSE_TIP_IDX = 1
_LEFT_EYE_IDX = 33
_RIGHT_EYE_IDX = 263
_MOUTH_CENTER_IDX = 13

# Default thresholds (calibrated for real-world phone selfies)
_DEFAULT_THRESHOLDS: Dict[str, float] = {
    "face_confidence": 0.50,
    "max_yaw": 40.0,
    "max_pitch": 35.0,
    "min_blur": 25.0,
    "min_brightness": 30.0,
    "max_brightness": 235.0,
    "min_face_size": 100.0,
    "min_landmark_visibility": 0.70,
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class QualityResult:
    """Outcome of a single quality check."""

    passed: bool
    check_name: str
    score: float        # 0-1, how well the image passed this check
    message: str        # user-facing message (always populated)


@dataclass
class QualityReport:
    """Aggregated outcome of all quality checks for one image."""

    passed: bool                     # True only when every check passed
    results: List[QualityResult] = field(default_factory=list)
    failed_checks: List[str] = field(default_factory=list)
    guidance: str = ""               # combined guidance for all failures


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def load_thresholds(config_path: Optional[Path] = None) -> Dict[str, float]:
    """Load quality thresholds from *api_config.yaml*, falling back to
    compiled-in defaults when the file is absent or malformed."""

    path = config_path or _DEFAULT_CONFIG_PATH
    thresholds = dict(_DEFAULT_THRESHOLDS)

    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
            if isinstance(raw, dict) and "quality_thresholds" in raw:
                for key, value in raw["quality_thresholds"].items():
                    if key in thresholds:
                        thresholds[key] = float(value)
                logger.debug("Loaded quality thresholds from %s", path)
        except Exception:
            logger.warning(
                "Could not parse %s; using default thresholds.", path,
                exc_info=True,
            )
    return thresholds


def _thresholds_from_config(config: Optional[dict] = None) -> Dict[str, float]:
    """Resolve thresholds: explicit dict > api_config.yaml > defaults."""
    if config is not None:
        merged = dict(_DEFAULT_THRESHOLDS)
        merged.update({k: float(v) for k, v in config.items()})
        return merged
    return load_thresholds()


# ---------------------------------------------------------------------------
# MediaPipe Face Detection & Landmarks
# ---------------------------------------------------------------------------
# Delegates to face_alignment.get_landmarks_robust, which already handles:
#   - Solutions API (mp.solutions.face_mesh) when available
#   - Tasks API (mp.tasks.vision.FaceLandmarker) fallback for mediapipe
#     builds where mp.solutions was removed (e.g. Python 3.14+)
#   - 0/90/180/270 deg rotation retries
# This avoids duplicating (and diverging from) that fallback logic here.

def _extract_landmarks_and_bbox(image: np.ndarray) -> Tuple[Optional[np.ndarray], Optional[Tuple[int, int, int, int]]]:
    """Extract (N, 2) pixel coordinates landmarks and (x, y, w, h) bounding box."""
    if image is None or image.size == 0:
        return None, None

    from .face_alignment import get_landmarks_robust

    h, w = image.shape[:2]
    _, lms = get_landmarks_robust(image)

    if lms is None:
        return None, None

    min_x = max(0, int(lms[:, 0].min()))
    min_y = max(0, int(lms[:, 1].min()))
    max_x = min(w, int(lms[:, 0].max()))
    max_y = min(h, int(lms[:, 1].max()))
    bbox = (min_x, min_y, max(1, max_x - min_x), max(1, max_y - min_y))

    return lms, bbox


# ---------------------------------------------------------------------------
# Individual quality checks
# ---------------------------------------------------------------------------

def check_face_detected(
    image: np.ndarray,
    *,
    threshold: float = _DEFAULT_THRESHOLDS["face_confidence"],
) -> Tuple[QualityResult, Optional[Tuple[int, int, int, int]]]:
    """Detect a face and extract face bounding box."""
    check_name = "face_detected"
    fail_msg = "Could not detect a face. Please ensure your face is clearly visible."

    if image is None or image.size == 0:
        return QualityResult(passed=False, check_name=check_name, score=0.0, message=fail_msg), None

    _, bbox = _extract_landmarks_and_bbox(image)
    if bbox is None:
        return QualityResult(passed=False, check_name=check_name, score=0.0, message=fail_msg), None

    return QualityResult(passed=True, check_name=check_name, score=1.0, message="Face detected."), bbox


def check_face_angle(
    landmarks: np.ndarray,
    *,
    max_yaw: float = _DEFAULT_THRESHOLDS["max_yaw"],
    max_pitch: float = _DEFAULT_THRESHOLDS["max_pitch"],
) -> QualityResult:
    """Estimate head yaw and pitch from Face Mesh landmarks using 3D perspective projection."""
    check_name = "face_angle"

    left_cheek = landmarks[_LEFT_CHEEK_IDX]
    right_cheek = landmarks[_RIGHT_CHEEK_IDX]
    nose_tip = landmarks[_NOSE_TIP_IDX]
    forehead = landmarks[_FOREHEAD_IDX]
    chin = landmarks[_CHIN_IDX]

    # --- Yaw estimation (perspective projection: ratio = (1 - sin(yaw)) / (1 + sin(yaw))) ---
    dist_left = float(np.linalg.norm(nose_tip - left_cheek))
    dist_right = float(np.linalg.norm(nose_tip - right_cheek))
    if max(dist_left, dist_right) < 1e-6:
        ratio_lr = 1.0
    else:
        ratio_lr = min(dist_left, dist_right) / max(dist_left, dist_right)

    sin_yaw = max(min((1.0 - ratio_lr) / max(1.0 + ratio_lr, 1e-6), 1.0), 0.0)
    yaw_deg = float(math.degrees(math.asin(sin_yaw)))

    # --- Pitch estimation ---
    dist_up = float(np.linalg.norm(nose_tip - forehead))
    dist_down = float(np.linalg.norm(nose_tip - chin))
    if max(dist_up, dist_down) < 1e-6:
        ratio_ud = 1.0
    else:
        ratio_ud = min(dist_up, dist_down) / max(dist_up, dist_down)

    sin_pitch = max(min((1.0 - ratio_ud) / max(1.0 + ratio_ud, 1e-6), 1.0), 0.0)
    pitch_deg = float(math.degrees(math.asin(sin_pitch)))

    passed = yaw_deg <= max_yaw and pitch_deg <= max_pitch
    yaw_score = max(1.0 - yaw_deg / max_yaw, 0.0) if max_yaw > 0 else 1.0
    pitch_score = max(1.0 - pitch_deg / max_pitch, 0.0) if max_pitch > 0 else 1.0
    score = min(yaw_score, pitch_score)

    message = "Face angle acceptable." if passed else "Please face the camera more directly."

    return QualityResult(passed=passed, check_name=check_name, score=score, message=message)


def check_blur(
    image: np.ndarray,
    face_bbox: Tuple[int, int, int, int],
    *,
    min_variance: float = _DEFAULT_THRESHOLDS["min_blur"],
) -> QualityResult:
    """Compute Laplacian variance on the face region."""
    check_name = "blur"

    x, y, w, h = face_bbox
    face_crop = image[y : y + h, x : x + w]

    if face_crop.size == 0:
        return QualityResult(
            passed=False,
            check_name=check_name,
            score=0.0,
            message="Image is too blurry. Hold your phone steady.",
        )

    gray = cv2.cvtColor(face_crop, cv2.COLOR_BGR2GRAY)
    variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    score = min(variance / (min_variance * 2.0), 1.0) if min_variance > 0 else 1.0
    passed = variance >= min_variance

    message = "Sharpness acceptable." if passed else "Image is too blurry. Hold your phone steady."

    return QualityResult(passed=passed, check_name=check_name, score=score, message=message)


def check_brightness(
    image: np.ndarray,
    face_bbox: Tuple[int, int, int, int],
    *,
    min_brightness: float = _DEFAULT_THRESHOLDS["min_brightness"],
    max_brightness: float = _DEFAULT_THRESHOLDS["max_brightness"],
) -> QualityResult:
    """Compute mean L* (CIELAB lightness) of the face region."""
    check_name = "brightness"

    x, y, w, h = face_bbox
    face_crop = image[y : y + h, x : x + w]

    if face_crop.size == 0:
        return QualityResult(
            passed=False,
            check_name=check_name,
            score=0.0,
            message="Image is too dark. Move to better lighting.",
        )

    lab = cv2.cvtColor(face_crop, cv2.COLOR_BGR2LAB)
    mean_l = float(lab[:, :, 0].mean())

    if mean_l < min_brightness:
        score = mean_l / min_brightness if min_brightness > 0 else 0.0
        return QualityResult(passed=False, check_name=check_name, score=max(score, 0.0), message="Image is too dark. Move to better lighting.")

    if mean_l > max_brightness:
        headroom = 255.0 - max_brightness
        score = (255.0 - mean_l) / headroom if headroom > 0 else 0.0
        return QualityResult(passed=False, check_name=check_name, score=max(score, 0.0), message="Image is too bright. Avoid direct light.")

    mid = (min_brightness + max_brightness) / 2.0
    half_range = (max_brightness - min_brightness) / 2.0
    score = 1.0 - abs(mean_l - mid) / half_range if half_range > 0 else 1.0

    return QualityResult(passed=True, check_name=check_name, score=float(score), message="Brightness acceptable.")


def check_resolution(
    face_bbox: Tuple[int, int, int, int],
    *,
    min_face_size: float = _DEFAULT_THRESHOLDS["min_face_size"],
) -> QualityResult:
    """Verify the face bounding box is at least min_face_size pixels."""
    check_name = "resolution"

    _, _, w, h = face_bbox
    min_dim = min(w, h)

    passed = min_dim >= min_face_size
    score = min(min_dim / min_face_size, 1.0) if min_face_size > 0 else 1.0
    message = "Face resolution acceptable." if passed else "Please move your camera closer."

    return QualityResult(passed=passed, check_name=check_name, score=score, message=message)


def check_occlusion(
    image: np.ndarray,
    landmarks: Optional[np.ndarray] = None,
    *,
    min_visibility: float = _DEFAULT_THRESHOLDS["min_landmark_visibility"],
) -> Tuple[QualityResult, Optional[np.ndarray]]:
    """Verify facial landmarks exist and key zones are unobstructed."""
    check_name = "occlusion"
    fail_msg = "Please remove sunglasses, masks, or hair covering your face."

    if landmarks is None:
        landmarks, _ = _extract_landmarks_and_bbox(image)

    if landmarks is None or len(landmarks) < 468:
        return QualityResult(passed=False, check_name=check_name, score=0.0, message=fail_msg), None

    h, w = image.shape[:2]
    # Check that landmark points are well distributed within the image bounds
    in_bounds = (landmarks[:, 0] >= 0) & (landmarks[:, 0] < w) & (landmarks[:, 1] >= 0) & (landmarks[:, 1] < h)
    in_bounds_fraction = float(in_bounds.mean())

    passed = in_bounds_fraction >= 0.85
    message = "Landmark visibility acceptable." if passed else fail_msg

    return QualityResult(passed=passed, check_name=check_name, score=in_bounds_fraction, message=message), landmarks


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def validate_image(
    image: np.ndarray,
    config: Optional[dict] = None,
) -> QualityReport:
    """Run all quality checks on image and return a complete report."""
    thresholds = _thresholds_from_config(config)
    results: List[QualityResult] = []

    # 1. Extract landmarks and bbox once
    landmarks, face_bbox = _extract_landmarks_and_bbox(image)

    # 1. Face detection
    if face_bbox is not None:
        face_result = QualityResult(passed=True, check_name="face_detected", score=1.0, message="Face detected.")
    else:
        face_result = QualityResult(
            passed=False,
            check_name="face_detected",
            score=0.0,
            message="Could not detect a face. Please ensure your face is clearly visible.",
        )
    results.append(face_result)

    # 2. Occlusion
    occlusion_result, landmarks = check_occlusion(
        image, landmarks=landmarks, min_visibility=thresholds["min_landmark_visibility"],
    )
    results.append(occlusion_result)

    # 3. Face angle
    if landmarks is not None:
        angle_result = check_face_angle(
            landmarks,
            max_yaw=thresholds["max_yaw"],
            max_pitch=thresholds["max_pitch"],
        )
    else:
        angle_result = QualityResult(
            passed=False,
            check_name="face_angle",
            score=0.0,
            message="Please face the camera more directly.",
        )
    results.append(angle_result)

    # 4. Blur
    if face_bbox is not None:
        blur_result = check_blur(
            image, face_bbox, min_variance=thresholds["min_blur"],
        )
    else:
        blur_result = QualityResult(
            passed=False,
            check_name="blur",
            score=0.0,
            message="Image is too blurry. Hold your phone steady.",
        )
    results.append(blur_result)

    # 5. Brightness
    if face_bbox is not None:
        brightness_result = check_brightness(
            image,
            face_bbox,
            min_brightness=thresholds["min_brightness"],
            max_brightness=thresholds["max_brightness"],
        )
    else:
        brightness_result = QualityResult(
            passed=False,
            check_name="brightness",
            score=0.0,
            message="Image is too dark. Move to better lighting.",
        )
    results.append(brightness_result)

    # 6. Resolution
    if face_bbox is not None:
        resolution_result = check_resolution(
            face_bbox, min_face_size=thresholds["min_face_size"],
        )
    else:
        resolution_result = QualityResult(
            passed=False,
            check_name="resolution",
            score=0.0,
            message="Please move your camera closer.",
        )
    results.append(resolution_result)

    # --- Aggregate ---
    failed_checks = [r.check_name for r in results if not r.passed]
    all_passed = len(failed_checks) == 0

    guidance_parts = [r.message for r in results if not r.passed]
    guidance = " ".join(guidance_parts) if guidance_parts else "All checks passed."

    return QualityReport(
        passed=all_passed,
        results=results,
        failed_checks=failed_checks,
        guidance=guidance,
    )
