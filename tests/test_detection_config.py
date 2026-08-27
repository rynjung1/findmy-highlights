"""Explicit tests for DetectionConfig's real, live defaults -- specifically
model_variant, so a future change back to "base" (or to anything else) is
a visible, deliberate diff against a real assertion, not a silent default
change nobody notices. See pipeline/detection.py's own docstring and the
2026-08-27 frame-verification in docs/INVESTIGATION_LOG.md for why "small"
is the real, evidence-backed default now."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.detection import (DetectionConfig, _MODEL_CLASSES,
                                _resolve_model_class)


def test_default_model_variant_is_small():
    assert DetectionConfig().model_variant == "small"


def test_model_variant_resolves_to_the_right_rfdetr_class_name():
    # Checks the real mapping this project's cache key and detect_persons
    # both depend on -- this one reads the plain dict directly, no import
    # of torch/rfdetr needed (unlike the next test, which calls
    # _resolve_model_class and does pay that real import cost).
    assert _MODEL_CLASSES == {
        "base": "RFDETRBase", "medium": "RFDETRMedium",
        "small": "RFDETRSmall", "nano": "RFDETRNano",
    }


def test_unknown_model_variant_raises_a_real_error():
    import pytest
    with pytest.raises(ValueError, match="unknown model_variant"):
        _resolve_model_class("giant")
