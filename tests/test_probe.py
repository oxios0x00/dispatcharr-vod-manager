import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from probe import classify_quality


def test_standard_2160p():
    assert classify_quality(3840, 2160) == "2160p"


def test_cinemascope_4k_with_short_height_is_still_2160p():
    # Real bug, found live on Dispatcharr-test (Ted Lasso): a 2:1
    # cinemascope UHD master is 3840x1920 — genuine 4K (confirmed by
    # Emby), but a height-only check (height >= 2000) misclassified it
    # as 1080p because 1920 < 2000.
    assert classify_quality(3840, 1920) == "2160p"


def test_standard_1080p():
    assert classify_quality(1920, 1080) == "1080p"


def test_ultrawide_1080p_by_width_not_height():
    assert classify_quality(1920, 800) == "1080p"


def test_standard_720p():
    assert classify_quality(1280, 720) == "720p"


def test_standard_480p():
    assert classify_quality(854, 480) == "480p"


def test_below_480p_is_sd():
    assert classify_quality(426, 240) == "sd"


def test_no_dimensions_is_unknown():
    assert classify_quality(None, None) == "unknown"
    assert classify_quality(0, 0) == "unknown"


def test_only_width_known_still_classifies():
    assert classify_quality(3840, None) == "2160p"


def test_only_height_known_still_classifies():
    assert classify_quality(None, 2160) == "2160p"


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"OK   {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
