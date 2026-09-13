"""Video encoders and the single frame renderer they share.

* MJPG AVI through ``cv2.VideoWriter`` (plays in Windows' own tools, large files).
* H.264 MP4 through the ``imageio_ffmpeg`` binary (libx264, veryfast, crf 18, yuv420p,
  ``+faststart``), fed raw RGB on stdin exactly as Codex 0.3 did.

Both containers of one video are written from **one** render pass: at 1900 px a CD14 position
takes minutes per pass, so rendering twice (Codex's ``mp4_copies``) is not acceptable.

Every pixel is composed by :mod:`etaluma_video.engine.display`; this module only reads planes,
asks display for the RGB frame, asks :mod:`etaluma_video.engine.reports` for the optional
scale bar/label, and pushes bytes into the encoders.
"""
from __future__ import annotations

import math
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import display
from .jobs import ProgressFn, check_cancel
from .parsing import Dataset, read_plane
from .reports import annotate_image

__all__ = ["playback_fps", "render_video_frame", "export_video", "video_frame", "VIDEO_VARIANTS"]

#: Composite variants; any other variant string must be a channel name.
VIDEO_VARIANTS = ("composite", "fluorescence_only")


def playback_fps(ds: Dataset, opts, first, n_frames: int) -> float:
    """Frames per second for one video: total duration by default (decision: 10 s playback)."""
    if opts.playback_source == "duration":
        return opts.fps or n_frames / opts.duration_seconds
    if opts.playback_source == "fps":
        return opts.fps or 10.0
    av = next((a for a in ds.avs if a["roi"] == first.roi and a["channel"] == first.channel), {})
    video_length = float(ds.protocol.get("raw", {}).get("videoLengthSeconds", 5) or 5)
    duration_fps = n_frames / max(video_length, 1)
    fps = opts.fps or (av.get("fps") or duration_fps if opts.playback_source == "avs" else duration_fps)
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("Video frame rate from metadata is invalid; supply an explicit FPS.")
    return fps


def _fourcc(code: str = "MJPG") -> int:
    """OpenCV 5 moved ``VideoWriter_fourcc`` onto the class; accept either spelling."""
    maker = getattr(cv2, "VideoWriter_fourcc", None) or cv2.VideoWriter.fourcc
    return maker(*code)


def render_video_frame(record: dict, ds: Dataset, profile, bounds, variant: str = "composite",
                       width: int | None = None, *, pixel_size: float | None = None,
                       scale_bar: bool = False, channel_label: bool = False,
                       interval_seconds: float | None = None, timestamp: bool = False) -> Image.Image:
    """One video frame as a PIL image.

    record: ``{channel: Frame}`` for one timepoint.
    variant: ``"composite"`` (fluorescence over the WHITE underlay), ``"fluorescence_only"``,
    or a channel name for the single-channel video.
    """
    planes = {ch: read_plane(f.path, ch) for ch, f in record.items()}
    if not planes or len({a.shape for a in planes.values()}) != 1:
        raise ValueError("Composite channels must have identical image dimensions.")
    if variant in VIDEO_VARIANTS:
        rgb = display.render_frame(planes, profile, bounds, include_white=(variant == "composite"))
    elif variant in planes:
        rgb = display.render_single_channel(planes[variant], variant, profile, bounds)
    else:
        raise ValueError(f"Unknown video variant {variant!r}")
    first = next(iter(record.values()))
    labels = [" + ".join(record)] if channel_label else []
    if timestamp:
        if not interval_seconds:
            raise ValueError("Supply the capture interval before enabling elapsed timestamps.")
        labels.append(f"{first.serial * interval_seconds / 3600:.2f} h")
    size = width if width is not None else rgb.shape[1]
    size = max(2, int(size) // 2 * 2)
    img = annotate_image(rgb, size, pixel_size, scale_bar and bool(pixel_size), " · ".join(labels))
    if img.height % 2:
        img = img.crop((0, 0, img.width, img.height // 2 * 2))
    img.info["source_shape"] = next(iter(planes.values())).shape
    return img


def avi_writer(path, fps: float, size):
    """MJPG AVI writer: OpenCV's default backend, else its own MJPEG encoder (1.0)."""
    writer = cv2.VideoWriter(str(path), _fourcc("MJPG"), fps, size)
    api = getattr(cv2, "CAP_OPENCV_MJPEG", None)
    if not writer.isOpened() and api is not None:
        writer.release()
        writer = cv2.VideoWriter(str(path), api, _fourcc("MJPG"), fps, size)
    return writer


def open_capture(path):
    """``cv2.VideoCapture`` for a video the app wrote, on whichever backend can read it (1.0).

    OpenCV's default backends first (its FFmpeg plug-in), then its own MJPEG reader for AVI files and
    Windows Media Foundation.
    Returns an unopened capture when nothing can read the file; callers check ``isOpened()``.
    """
    path = str(path)
    backends = [cv2.CAP_ANY]
    if path.lower().endswith(".avi") and hasattr(cv2, "CAP_OPENCV_MJPEG"):
        backends.append(cv2.CAP_OPENCV_MJPEG)
    if hasattr(cv2, "CAP_MSMF"):
        backends.append(cv2.CAP_MSMF)
    capture = None
    for api in backends:
        if capture is not None:
            capture.release()
        capture = cv2.VideoCapture(path, api)
        if capture.isOpened():
            return capture
    return capture


def _verify_video(path: Path, expected_frames: int) -> None:
    capture = open_capture(path)
    try:
        count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        ok, _ = capture.read()
        if not ok or count != expected_frames:
            raise RuntimeError(f"Video verification failed for {path.name}: {count}/{expected_frames} frames")
    finally:
        capture.release()


#: MP4 quality presets (decision 2): x264 CRF, and bytes per rendered pixel per frame measured on
#: the CD14 sample's composites at 1900 px (noisy phase contrast plus rolling-ball fluorescence).
VIDEO_QUALITY = {"high": 18, "standard": 23, "small": 28}
MP4_BYTES_PER_PIXEL = {"high": 0.26, "standard": 0.11, "small": 0.03}


class _Mp4Pipe:
    """ffmpeg stdin pipe; a disk-backed stderr sink keeps encoder errors from blocking it."""

    def __init__(self, path: Path, size: tuple[int, int], fps: float, crf: int = 23, threads: int = 0) -> None:
        import imageio_ffmpeg

        self.path = path
        self.size = size
        self.errors = tempfile.TemporaryFile(mode="w+b")
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "rawvideo", "-vcodec", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "pipe:0", "-an",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)), "-pix_fmt", "yuv420p",
                   # 0.5 pinned x264 to 2 threads, which capped a 1900 px stream at about 14 fps
                   "-movflags", "+faststart", "-threads", str(int(threads)), str(path)]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=self.errors, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _fail(self, exc: BaseException | None = None) -> None:
        self.errors.seek(0)
        message = "MP4 encoder failed: " + self.errors.read().decode(errors="replace")[-2000:]
        raise RuntimeError(message) from exc

    def write(self, img: Image.Image) -> None:
        try:
            self.process.stdin.write(img.tobytes())
        except (BrokenPipeError, OSError) as exc:
            self._fail(exc)

    def close(self, cancel=None) -> None:
        self.process.stdin.close()
        while True:
            check_cancel(cancel)
            try:
                returncode = self.process.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if returncode:
            self._fail()
        self.errors.close()

    def kill(self) -> None:
        try:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
        except OSError:
            pass
        finally:
            self.errors.close()


def export_video(base_path: Path | str, records: list[dict], ds: Dataset, opts, profile, bounds, *,
                 variant: str, pixel_size: float | None = None, interval_seconds: float | None = None,
                 avi: bool = True, mp4: bool = True, fps: float | None = None,
                 progress: ProgressFn | None = None, cancel=None) -> list[dict]:
    """Render ``records`` once and write the requested containers next to each other.

    base_path has no suffix; ``<base>.avi`` and/or ``<base>.mp4`` are produced. Returns one
    metadata dict per written file.
    """
    base_path = Path(base_path)
    base_path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        raise ValueError("No synchronized frames available for this video.")
    if not (avi or mp4):
        return []
    first = next(iter(records[0].values()))
    fps = fps if fps is not None else playback_fps(ds, opts, first, len(records))
    avi_path, mp4_path = base_path.with_suffix(".avi"), base_path.with_suffix(".mp4")
    writer = None
    pipe = None
    expected_size = None
    source_shape = None
    try:
        for i, record in enumerate(records):
            check_cancel(cancel)
            img = render_video_frame(record, ds, profile, bounds, variant, opts.width,
                                     pixel_size=pixel_size, scale_bar=opts.app_scale_bar,
                                     channel_label=opts.channel_label, interval_seconds=interval_seconds,
                                     timestamp=opts.app_timestamp)
            if source_shape is not None and img.info["source_shape"] != source_shape:
                raise ValueError(f"Image dimensions change within {base_path.name}")
            source_shape = img.info["source_shape"]
            if expected_size is None:
                expected_size = img.size
                if avi:
                    writer = avi_writer(avi_path, fps, expected_size)
                    if not writer.isOpened():
                        raise RuntimeError(f"MJPG writer unavailable: {avi_path}")
                if mp4:
                    pipe = _Mp4Pipe(mp4_path, expected_size, fps)
            if img.size != expected_size:
                raise ValueError(f"Image dimensions change within {base_path.name}")
            if writer is not None:
                writer.write(cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR))
            if pipe is not None:
                pipe.write(img)
            if progress and (i % 25 == 0 or i == len(records) - 1):
                progress(f"{base_path.name}: {i + 1}/{len(records)} frames")
        if writer is not None:
            writer.release()
            writer = None
        if pipe is not None:
            pipe.close(cancel)
            pipe = None
    except BaseException:
        if writer is not None:
            writer.release()
        if pipe is not None:
            pipe.kill()
        avi_path.unlink(missing_ok=True)
        mp4_path.unlink(missing_ok=True)
        raise
    check_cancel(cancel)
    written = []
    for path, wanted in ((avi_path, avi), (mp4_path, mp4)):
        if not wanted:
            continue
        _verify_video(path, len(records))
        written.append({"file": path.name, "variant": variant, "frames": len(records), "fps": fps,
                        "size": list(expected_size), "bytes": path.stat().st_size,
                        "serials": [next(iter(r.values())).serial for r in records]})
    return written


def video_frame(path: Path | str, index: int) -> tuple[np.ndarray, int, float]:
    """(RGB frame, frame count, fps) decoded from a written video; used by tests and the viewer."""
    capture = open_capture(path)
    try:
        count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = capture.get(cv2.CAP_PROP_FPS)
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = capture.read()
        if not ok:
            raise ValueError(f"Could not decode frame {index}: {path}")
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), count, fps
    finally:
        capture.release()
