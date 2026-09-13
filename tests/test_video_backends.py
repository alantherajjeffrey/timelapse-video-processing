"""1.0: an MJPG AVI is written and read back; the diagnostic self-test of the trimmed bundle passes."""
from __future__ import annotations

import numpy as np


def test_mjpg_avi_is_written_and_read_back(tmp_path):
    import cv2

    from etaluma_video.engine.export import avi_writer, open_capture

    path = tmp_path / "x.avi"
    writer = avi_writer(path, 5.0, (64, 48))
    assert writer.isOpened()
    for i in range(4):
        writer.write(np.full((48, 64, 3), i * 40, np.uint8))
    writer.release()
    capture = open_capture(path)
    assert capture.isOpened() and int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == 4
    ok, frame = capture.read()
    capture.release()
    assert ok and frame.shape == (48, 64, 3)


def test_open_capture_reports_an_unreadable_file(tmp_path):
    from etaluma_video.engine.export import open_capture

    path = tmp_path / "broken.mp4"
    path.write_bytes(b"not a video")
    capture = open_capture(path)
    assert capture is not None and not capture.isOpened()


def test_diagnostic_selftest_passes():
    from etaluma_video.__main__ import _selftest

    assert _selftest() == {"tiff_lzw": "ok", "avi_mjpg": "ok", "mp4_h264": "ok"}
