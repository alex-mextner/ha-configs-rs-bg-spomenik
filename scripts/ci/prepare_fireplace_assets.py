#!/usr/bin/env python3
# Copyright (c) 2026 The ha-configs-rs-bg-spomenik contributors
"""Build and validate fireplace candidates on a hosted GitHub Actions runner."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from types import FrameType

THREADS = 2
CHANNELS = 2
SOURCE_COUNT = 2
MEMORY_BYTES = 4 * 1024**3
MIN_FREE_BYTES = 6 * 1024**3
MAX_SOURCE_BYTES = 1200 * 1024**2
MAX_LOG_BYTES = 8 * 1024**2
BUILD_SECONDS = 2400
COMMAND_SECONDS = 1200
DOWNLOAD_SECONDS = 600
HEARTBEAT_SECONDS = 20
FPS = 30
SAMPLE_RATE = 48000
SEAM_SECONDS = 1
DURATION_TOLERANCE = 0.15
SOURCE_DURATION_TOLERANCE = 0.25
TARGET_I = -24.0
TARGET_TP = -2.5
TARGET_LRA = 7.0
FINAL_TP_MAX = -2.0
FINAL_LRA_MAX = 10.0
LOUDNESS_TOLERANCE = 1.0
ALLOWED_HOSTS = frozenset({"www.nps.gov", "videos.pexels.com"})
LOG = logging.getLogger("prepare-fireplace-assets")


class BuildError(Exception):
    """A failed contract with the real child or cancellation exit status."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        """Retain the operational status for the final receipt and caller."""
        super().__init__(message)
        self.exit_code = exit_code


def fail(message: str, *, exit_code: int = 1) -> NoReturn:
    """Reject a candidate with an actionable diagnostic."""
    raise BuildError(message, exit_code)


def valid_url(url: str) -> None:
    """Permit only the two fixed public HTTPS source origins."""
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        fail("Source URL is outside the fixed public HTTPS origins")


def sha256_file(path: Path) -> str:
    """Hash every byte of a complete file without loading it into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: Any) -> None:
    """Write an owned receipt atomically with private permissions."""
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def limited_command(arguments: list[str]) -> list[str]:
    """Apply kernel limits without executing Python between fork and exec."""
    cpus = ",".join(str(cpu) for cpu in sorted(os.sched_getaffinity(0))[:THREADS])
    return [
        "prlimit",
        f"--as={MEMORY_BYTES}",
        f"--cpu={COMMAND_SECONDS}",
        f"--fsize={2 * 1024**3}",
        "--",
        "taskset",
        "--cpu-list",
        cpus,
        *arguments,
    ]


def stop_child(process: subprocess.Popen[bytes]) -> None:
    """Reap only this builder's process group on failure or cancellation."""
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def command_result(log_path: Path, returncode: int, label: str) -> str:
    """Check completed logs before reading; retain only bounded failure diagnostics."""
    status = returncode if returncode > 0 else 128 - returncode
    if log_path.stat().st_size > MAX_LOG_BYTES:
        with log_path.open("rb") as stream:
            stream.seek(-min(4000, log_path.stat().st_size), os.SEEK_END)
            LOG.error(
                "stage=%s diagnostic_tail=%s",
                label,
                stream.read(4000).decode(errors="replace"),
            )
        fail(
            f"{label}: completed diagnostic log exceeded its byte limit",
            exit_code=status if returncode else 1,
        )
    with log_path.open("rb") as stream:
        content = stream.read(MAX_LOG_BYTES + 1)
    if len(content) > MAX_LOG_BYTES:
        fail(
            f"{label}: diagnostic log grew beyond its byte limit",
            exit_code=status if returncode else 1,
        )
    result = content.decode(errors="replace")
    if returncode:
        LOG.error("stage=%s diagnostic_tail=%s", label, result[-4000:])
        fail(f"{label}: command failed with exit {returncode}", exit_code=status)
    return result


class Builder:
    """Own bounded subprocesses, temporary sources, and candidate receipts."""

    def __init__(self, output: Path, log_dir: Path) -> None:
        """Initialize paths and a deadline without accepting existing outputs."""
        self.output = output
        self.log_dir = log_dir
        self.deadline = time.monotonic() + BUILD_SECONDS
        self.sequence = 0
        output.mkdir(parents=True, exist_ok=False)
        self.work = output / "work"
        self.work.mkdir()
        (output / "assets").mkdir()
        (output / "review").mkdir()

    def command(
        self, label: str, arguments: list[str], *, seconds: int = COMMAND_SECONDS
    ) -> str:
        """Run fixed tools with a supervised wall deadline and bounded diagnostics."""
        self.sequence += 1
        log_path = self.log_dir / f"{self.sequence:02d}-{label}.log"
        started = time.monotonic()
        deadline = min(self.deadline, started + seconds)
        if started >= deadline:
            fail(f"{label}: build wall deadline expired")
        LOG.info("stage=%s started command_log=%s", label, log_path)
        with log_path.open("xb") as command_log:
            process = subprocess.Popen(  # noqa: S603 - fixed tool argv; never a shell
                limited_command(arguments),
                stdin=subprocess.DEVNULL,
                stdout=command_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            heartbeat = started
            try:
                while process.poll() is None:
                    now = time.monotonic()
                    if now > deadline or log_path.stat().st_size > MAX_LOG_BYTES:
                        fail(f"{label}: runtime or diagnostic-size limit exceeded")
                    if now - heartbeat >= HEARTBEAT_SECONDS:
                        LOG.info("stage=%s running elapsed=%.1fs", label, now - started)
                        heartbeat = now
                    time.sleep(0.2)
            finally:
                stop_child(process)
                LOG.info(
                    "stage=%s completed rc=%s elapsed=%.1fs",
                    label,
                    process.returncode,
                    time.monotonic() - started,
                )
        return command_result(log_path, process.returncode, label)

    def download(self, source: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        """Supervise a bounded curl transfer and hash every complete source byte."""
        target = self.work / (source["id"] + "-source.mp4")
        expected = source["bytes"]
        started = time.monotonic()
        LOG.info("stage=download source=%s expected_bytes=%s", source["id"], expected)
        # Do not follow redirects; every request stays at the manifest's fixed origin.
        output = self.command(
            source["id"] + "-download",
            [
                "curl",
                "--disable",
                "--fail",
                "--silent",
                "--show-error",
                "--proto",
                "=https",
                "--connect-timeout",
                "30",
                "--max-time",
                str(DOWNLOAD_SECONDS),
                "--max-filesize",
                str(expected),
                "--user-agent",
                "KodiFireplaceAssets/1.0 (source verification)",
                "--output",
                str(target),
                "--write-out",
                "%{http_code}",
                "--url",
                source["url"],
            ],
            seconds=DOWNLOAD_SECONDS,
        )
        if output.strip() != "200":
            fail(f"{source['id']}: expected a direct HTTP 200 source response")
        count = target.stat().st_size
        if count != expected:
            fail(f"{source['id']}: size changed: expected={expected} actual={count}")
        actual_hash = sha256_file(target)
        if source["sha256"] is not None and actual_hash != source["sha256"]:
            fail(
                f"{source['id']}: pinned source SHA-256 mismatch: actual={actual_hash}"
            )
        LOG.info(
            "stage=download source=%s completed bytes=%s sha256=%s elapsed=%.1fs",
            source["id"],
            count,
            actual_hash,
            time.monotonic() - started,
        )
        return target, {
            "id": source["id"],
            "url": source["url"],
            "bytes": count,
            "sha256": actual_hash,
            "expected_sha256": source["sha256"],
            "checksum_status": "pinned-match" if source["sha256"] else "first-observed",
            "provenance": source["provenance"],
        }

    def probe(
        self, path: Path, label: str, *, count_frames: bool = False
    ) -> dict[str, Any]:
        """Inspect actual streams; the finished-file pass decodes every frame."""
        arguments = ["ffprobe", "-v", "error", "-threads", str(THREADS)]
        if count_frames:
            arguments += ["-count_frames"]
        arguments += ["-show_streams", "-show_format", "-of", "json", str(path)]
        return json.loads(self.command(label, arguments))

    def audio_metrics(
        self, arguments: list[str], graph: str, label: str
    ) -> dict[str, float]:
        """Measure the full audio timeline using loudnorm's input measurements."""
        output = self.command(
            label,
            ffmpeg()
            + arguments
            + [
                "-filter_complex",
                graph + ";[cycle]" + loudnorm() + "[measured]",
                "-map",
                "[measured]",
                "-f",
                "null",
                "-",
            ],
        )
        matches = re.findall(r'\{\s*"input_i"[\s\S]*?\}', output)
        if len(matches) != 1:
            fail(f"{label}: expected one complete loudness measurement")
        parsed = json.loads(matches[0])
        values = {
            key: float(parsed[key])
            for key in (
                "input_i",
                "input_tp",
                "input_lra",
                "input_thresh",
                "target_offset",
            )
        }
        if not all(math.isfinite(value) for value in values.values()):
            fail(f"{label}: silent or non-finite audio measurement")
        return values


def ffmpeg() -> list[str]:
    """Keep decoder, encoder, and filter concurrency bounded."""
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-v",
        "info",
        "-threads",
        str(THREADS),
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
        "-xerror",
    ]


def loudnorm(measured: dict[str, float] | None = None) -> str:
    """Use identical targets in analysis and dynamic rendering passes."""
    result = f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json"
    if measured is not None:
        for option, field in (
            ("measured_I", "input_i"),
            ("measured_TP", "input_tp"),
            ("measured_LRA", "input_lra"),
            ("measured_thresh", "input_thresh"),
            ("offset", "target_offset"),
        ):
            result += f":{option}={measured[field]}"
        result += ":linear=false"
    return result


def cycle_graph(duration: float, *, video: bool) -> str:
    """Play the body and tail/head blend; each loop resumes after the head."""
    seam = SEAM_SECONDS
    tail = duration - seam
    audio = (
        f"[0:a:0]aresample={SAMPLE_RATE},atrim=duration={duration:.6f},"
        "asetpts=PTS-STARTPTS,asplit=3[ab][at][ah];"
        f"[ab]atrim=start={seam}:end={tail:.6f},asetpts=PTS-STARTPTS[bodya];"
        f"[at]atrim=start={tail:.6f}:end={duration:.6f},asetpts=PTS-STARTPTS,"
        f"afade=t=out:st=0:d={seam}:curve=tri[taila];"
        f"[ah]atrim=end={seam},asetpts=PTS-STARTPTS,"
        f"afade=t=in:st=0:d={seam}:curve=tri[heada];"
        "[taila][heada]amix=inputs=2:normalize=0:duration=shortest:dropout_transition=0[seama];"
        "[bodya][seama]concat=n=2:v=0:a=1[cycle]"
    )
    if not video:
        return audio
    return (
        f"[0:v:0]fps={FPS},trim=duration={duration:.6f},setpts=PTS-STARTPTS,"
        "format=yuv420p,split=3[vb][vt][vh];"
        f"[vb]trim=start={seam}:end={tail:.6f},setpts=PTS-STARTPTS[bodyv];"
        f"[vt]trim=start={tail:.6f}:end={duration:.6f},setpts=PTS-STARTPTS[tailv];"
        f"[vh]trim=end={seam},setpts=PTS-STARTPTS[headv];"
        f"[tailv][headv]xfade=transition=fade:duration={seam}:offset=0[seamv];"
        "[bodyv][seamv]concat=n=2:v=1:a=0,format=yuv420p[vout];" + audio
    )


def streams(probe: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Require one real video stream and one real stereo audio stream."""
    videos = [s for s in probe["streams"] if s["codec_type"] == "video"]
    audios = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1 or len(audios) != 1:
        fail("Expected exactly one video and one audio stream")
    return videos[0], audios[0]


def validate_media(
    probe: dict[str, Any], source: dict[str, Any], duration: float, *, finished: bool
) -> dict[str, Any]:
    """Check actual codec, orientation, dimensions, sync, duration and frame count."""
    video, audio = streams(probe)
    if (
        video["codec_name"] != "h264"
        or audio["codec_name"] != "aac"
        or [video["width"], video["height"]] != source["dimensions"]
        or int(audio["sample_rate"]) != SAMPLE_RATE
        or audio["channels"] != CHANNELS
    ):
        fail(f"{source['id']}: unexpected codec, native dimensions or audio format")
    rotations = [video.get("tags", {}).get("rotate", 0)]
    rotations += [item.get("rotation", 0) for item in video.get("side_data_list", [])]
    if any(float(rotation) != 0 for rotation in rotations):
        fail(f"{source['id']}: rotated video is not a usable fireplace")
    actual_duration = float(probe["format"]["duration"])
    tolerance = DURATION_TOLERANCE if finished else SOURCE_DURATION_TOLERANCE
    if (
        not math.isfinite(actual_duration)
        or abs(actual_duration - duration) > tolerance
    ):
        fail(f"{source['id']}: duration differs from its source/transform contract")
    result = {
        "video_codec": video["codec_name"],
        "audio_codec": audio["codec_name"],
        "dimensions": [video["width"], video["height"]],
        "sample_rate": int(audio["sample_rate"]),
        "channels": audio["channels"],
        "duration_seconds": actual_duration,
    }
    if finished:
        frames = round(duration * FPS)
        if video["pix_fmt"] != "yuv420p" or int(video["nb_read_frames"]) != frames:
            fail(f"{source['id']}: pixel format or decoded frame count mismatch")
        video_end = float(video["duration"]) + float(video.get("start_time", 0))
        audio_end = float(audio["duration"]) + float(audio.get("start_time", 0))
        if (
            abs(video_end - audio_end) > DURATION_TOLERANCE
            or abs(float(video.get("start_time", 0))) > DURATION_TOLERANCE
            or abs(float(audio.get("start_time", 0))) > DURATION_TOLERANCE
        ):
            fail(f"{source['id']}: audio/video timeline does not stay synchronized")
        result["decoded_video_frames"] = frames
    return result


def validate_loudness(metrics: dict[str, float]) -> None:
    """Accept actual decoded AAC levels, never the encoder's predicted output."""
    if (
        abs(metrics["input_i"] - TARGET_I) > LOUDNESS_TOLERANCE
        or metrics["input_tp"] > FINAL_TP_MAX
        or metrics["input_lra"] > FINAL_LRA_MAX
    ):
        fail("Finished AAC failed integrated loudness, true-peak or range limits")


def prepare_asset(
    builder: Builder, source: dict[str, Any], original: Path
) -> dict[str, Any]:
    """Render and inspect one native-resolution candidate before final naming."""
    source_probe = builder.probe(original, source["id"] + "-source-probe")
    validate_media(source_probe, source, source["duration_seconds"], finished=False)
    # Snap both tracks to one 30fps timeline; 48000/30 gives an exact sample boundary.
    duration = math.floor(source["trim_duration_seconds"] * FPS) / FPS
    expected_duration = duration - SEAM_SECONDS
    inputs = [
        "-ss",
        str(source["trim_start_seconds"]),
        "-t",
        f"{duration:.6f}",
        "-i",
        str(original),
    ]
    measured = builder.audio_metrics(
        inputs, cycle_graph(duration, video=False), source["id"] + "-audio-pass1"
    )
    temporary = builder.work / (source["id"] + "-candidate.mp4")
    graph = (
        cycle_graph(duration, video=True)
        + ";[cycle]"
        + loudnorm(measured)
        + f",aresample={SAMPLE_RATE}[aout]"
    )
    builder.command(
        source["id"] + "-encode",
        ffmpeg()
        + inputs
        + [
            "-filter_complex",
            graph,
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "19",
            "-threads",
            str(THREADS),
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "cfr",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            "-t",
            f"{expected_duration:.6f}",
            "-map_metadata",
            "-1",
            "-movflags",
            "+faststart",
            str(temporary),
        ],
    )
    actual = validate_media(
        builder.probe(temporary, source["id"] + "-final-probe", count_frames=True),
        source,
        expected_duration,
        finished=True,
    )
    final_metrics = builder.audio_metrics(
        ["-i", str(temporary)], "[0:a:0]anull[cycle]", source["id"] + "-final-aac"
    )
    validate_loudness(final_metrics)
    final = builder.output / "assets" / (source["id"] + ".mp4")
    temporary.replace(final)
    for label, position in (
        ("start", 0.5),
        ("middle", expected_duration / 2),
        ("seam", expected_duration - 0.5),
    ):
        builder.command(
            source["id"] + "-frame-" + label,
            [
                *ffmpeg(),
                "-ss",
                str(position),
                "-i",
                str(final),
                "-frames:v",
                "1",
                "-vf",
                "scale=960:-2",
                "-an",
                "-update",
                "1",
                str(builder.output / "review" / (source["id"] + "-" + label + ".jpg")),
            ],
        )
    # Explicit tail/head inputs avoid ambiguous stream_loop plus input-seek behavior.
    preview_graph = (
        "[0:v:0]setpts=PTS-STARTPTS,scale=960:-2[v0];"
        "[0:a:0]asetpts=PTS-STARTPTS[a0];"
        "[1:v:0]setpts=PTS-STARTPTS,scale=960:-2[v1];"
        "[1:a:0]asetpts=PTS-STARTPTS[a1];"
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[pv][pa]"
    )
    builder.command(
        source["id"] + "-seam-preview",
        [
            *ffmpeg(),
            "-ss",
            str(expected_duration - 3),
            "-t",
            "3",
            "-i",
            str(final),
            "-t",
            "3",
            "-i",
            str(final),
            "-filter_complex",
            preview_graph,
            "-map",
            "[pv]",
            "-map",
            "[pa]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-threads",
            str(THREADS),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(builder.output / "review" / (source["id"] + "-seam.mp4")),
        ],
    )
    return {
        "id": source["id"],
        "path": str(final.relative_to(builder.output)),
        "bytes": final.stat().st_size,
        "sha256": sha256_file(final),
        "media": actual,
        "normalization_first_pass": measured,
        "decoded_aac_metrics": {
            key: final_metrics[key]
            for key in ("input_i", "input_tp", "input_lra", "input_thresh")
        },
        "transform": {
            "source_start_seconds": source["trim_start_seconds"],
            "source_window_seconds": duration,
            "fps": FPS,
            "cyclic_overlap_seconds": SEAM_SECONDS,
            "timeline": (
                f"body[{SEAM_SECONDS},D-{SEAM_SECONDS}) + "
                f"blend(tail[D-{SEAM_SECONDS},D),head[0,{SEAM_SECONDS})); "
                f"next={SEAM_SECONDS}"
            ),
        },
    }


def load_sources(path: Path) -> list[dict[str, Any]]:
    """Reject accidental edits that escape the bounded two-source recipe."""
    manifest = json.loads(path.read_text())
    sources = manifest["sources"]
    if manifest["schema_version"] != 1 or len(sources) != SOURCE_COUNT:
        fail("Expected the two-source version 1 manifest")
    if len({source["id"] for source in sources}) != len(sources):
        fail("Source identifiers must be distinct")
    for source in sources:
        valid_url(source["url"])
        if (
            not re.fullmatch(r"[a-z0-9-]{1,64}", source["id"])
            or not 0 < source["bytes"] <= MAX_SOURCE_BYTES
            or source["dimensions"] not in ([1920, 1080], [1280, 720])
            or source["trim_start_seconds"] < 0
            or source["trim_duration_seconds"] <= 2 * SEAM_SECONDS
            or source["trim_start_seconds"] + source["trim_duration_seconds"]
            > source["duration_seconds"] + SOURCE_DURATION_TOLERANCE
        ):
            fail("Source bounds, identifier, or dimensions are invalid")
        if source["sha256"] is None:
            if (
                source["id"] != "valley-forge-1080p"
                or not source["first_digest_from_ci"]
            ):
                fail("Only the documented first NPS digest may be unpinned")
        elif not re.fullmatch(r"[a-f0-9]{64}", source["sha256"]):
            fail("Pinned source digest must be SHA-256")
    return sources


def cancel(signum: int, _frame: FrameType | None) -> NoReturn:
    """Let normal finally blocks reap children and persist a failure receipt."""
    fail(f"Build cancelled by signal {signum}", exit_code=128 + signum)


def start_logging() -> tuple[Path, Path]:
    """Initialize private automatic logs before operational work."""
    os.umask(0o077)
    run_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_dir = Path(
        tempfile.mkdtemp(prefix=f"prepare-fireplace-assets-{run_stamp}-", dir="/tmp")
    )
    main_log = log_dir / "build.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(main_log), logging.StreamHandler()],
    )
    LOG.info("starting log=%s", main_log)
    return log_dir, main_log


def parse_args() -> argparse.Namespace:
    """Read the small CI entry-point interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-only", action="store_true")
    return parser.parse_args()


def initial_receipt() -> dict[str, Any]:
    """Declare provenance, resource limits, and the still-unapproved candidate state."""
    return {
        "schema_version": 1,
        "status": "running",
        "approved_for_catalog": False,
        "listening_verified": False,
        "sources": [],
        "assets": [],
        "ci": {
            key: os.environ.get(key, "")
            for key in (
                "GITHUB_REPOSITORY",
                "GITHUB_SHA",
                "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT",
                "GITHUB_WORKFLOW_REF",
                "RUNNER_ENVIRONMENT",
                "RUNNER_OS",
                "RUNNER_ARCH",
            )
        },
        "limits": {
            "threads": THREADS,
            "child_memory_bytes": MEMORY_BYTES,
            "total_seconds": BUILD_SECONDS,
            "command_seconds": COMMAND_SECONDS,
            "download_seconds": DOWNLOAD_SECONDS,
            "diagnostic_bytes": MAX_LOG_BYTES,
        },
        "audio_contract": {
            "integrated_lufs": TARGET_I,
            "tolerance_lu": LOUDNESS_TOLERANCE,
            "true_peak_max_dbtp": FINAL_TP_MAX,
            "lra_max_lu": FINAL_LRA_MAX,
        },
    }


def record_tools(builder: Builder, receipt: dict[str, Any]) -> None:
    """Record actual runner tools and require curl's streaming size enforcement."""
    receipt["ffmpeg_version"] = builder.command(
        "ffmpeg-version", ["ffmpeg", "-version"]
    )
    curl_version = builder.command("curl-version", ["curl", "--disable", "--version"])
    receipt["curl_version"] = curl_version
    match = re.match(r"curl (\d+)\.(\d+)\.(\d+)", curl_version)
    if match is None or tuple(map(int, match.groups())) < (8, 4, 0):
        fail(
            "curl 8.4.0 or later is required to bound transfers without Content-Length"
        )


def run_build(builder: Builder, receipt: dict[str, Any], *, source_only: bool) -> None:
    """Download and prepare the fixed sources, persisting full-source hashes first."""
    manifest = Path(__file__).with_name("fireplace-sources.json")
    sources = load_sources(manifest)
    receipt["manifest_sha256"] = sha256_file(manifest)
    receipt["builder_sha256"] = sha256_file(Path(__file__))
    record_tools(builder, receipt)
    shutil.copyfile(manifest, builder.output / "sources.json")
    for source in sources:
        original, source_receipt = builder.download(source)
        receipt["sources"].append(source_receipt)
        write_json(builder.output / "receipt.json", receipt)
        if not source_only:
            receipt["assets"].append(prepare_asset(builder, source, original))
        original.unlink()
    receipt["status"] = "sources-verified" if source_only else "candidates-validated"
    receipt["unpinned_source_ids"] = [
        source["id"]
        for source in receipt["sources"]
        if source["checksum_status"] == "first-observed"
    ]
    receipt["review_files"] = [
        {
            "path": str(path.relative_to(builder.output)),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted((builder.output / "review").iterdir())
    ]


def finalization_error(
    receipt: dict[str, Any], outcome: int, stage: str, error: Exception
) -> int:
    """Record a best-effort finalization failure without replacing a prior status."""
    status = outcome or (error.exit_code if isinstance(error, BuildError) else 1)
    receipt["status"] = "failed"
    receipt["exit_code"] = status
    receipt.setdefault("finalization_errors", []).append(
        {"stage": stage, "type": type(error).__name__, "detail": str(error)}
    )
    LOG.exception("stage=%s failed during finalization rc=%s", stage, status)
    return status


def finalize_build(
    builder: Builder | None, receipt: dict[str, Any], outcome: int, started: float
) -> int:
    """Attempt owned cleanup and a final receipt independently, preserving failures."""
    if builder is not None:
        try:
            shutil.rmtree(builder.work)
        except (BuildError, OSError) as error:
            outcome = finalization_error(receipt, outcome, "cleanup", error)
    receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
    receipt["exit_code"] = outcome
    if builder is not None:
        try:
            write_json(builder.output / "receipt.json", receipt)
        except (BuildError, OSError, TypeError, ValueError) as error:
            outcome = finalization_error(receipt, outcome, "receipt", error)
        else:
            LOG.info("receipt=%s", builder.output / "receipt.json")
    # Workflow uploads the original private /tmp logs even when receipt writes fail.
    return outcome


def main() -> int:
    """Write automatic private logs, bounded candidates, and exact-byte receipts."""
    args = parse_args()
    log_dir, main_log = start_logging()
    started, outcome = time.monotonic(), 1
    builder = None
    receipt = initial_receipt()
    try:
        if (
            os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        ):
            fail("Run media builds only in the genuine hosted GitHub Actions workflow")
        signal.signal(signal.SIGTERM, cancel)
        signal.signal(signal.SIGINT, cancel)
        if shutil.disk_usage(args.output_dir.parent).free < MIN_FREE_BYTES:
            fail(
                "CI runner needs at least 6 GiB free for owned source and output files"
            )
        builder = Builder(args.output_dir, log_dir)
        run_build(builder, receipt, source_only=args.source_only)
        outcome = 0
    except (
        BuildError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        outcome = error.exit_code if isinstance(error, BuildError) else 1
        LOG.exception("build failed; no candidate approval was granted")
        receipt["status"] = "failed"
        receipt["error"] = {"type": type(error).__name__, "detail": str(error)}
    finally:
        outcome = finalize_build(builder, receipt, outcome, started)
        LOG.info(
            "completed rc=%s elapsed=%.1fs log=%s",
            outcome,
            time.monotonic() - started,
            main_log,
        )
    return outcome


if __name__ == "__main__":
    sys.exit(main())
