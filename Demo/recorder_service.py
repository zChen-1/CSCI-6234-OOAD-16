import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict

'''
REPLAY_MONITOR_INTERVAL_SECONDS ------ How often to rescan the camera list
REPLAY_SEGMENT_SECONDS --------------- Length of each MP4 clip
REPLAY_WINDOW_HOURS ------------------ How many hours of clips to keep
isoformat_z -------------------------- Converts datetime to UTC ISO strings ending in Z
load_managed_cameras ----------------- Loads the current set of cameras that should be recorded
replay_camera_dir -------------------- Returns the base recording directory for the camera
replay_clips_dir --------------------- Returns the clip folder for a camera
'''
from app import (
    REPLAY_MONITOR_INTERVAL_SECONDS,
    REPLAY_SEGMENT_SECONDS,
    REPLAY_WINDOW_HOURS,
    isoformat_z,
    load_managed_cameras,
    replay_camera_dir,
    replay_clips_dir,
)

UTC = timezone.utc
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
RESTART_DELAY_SECONDS = max(1, int(os.getenv("REPLAY_RESTART_DELAY_SECONDS", "2")))
MIN_CLIP_BYTES = max(1024, int(os.getenv("REPLAY_MIN_CLIP_BYTES", "65536")))
LOG_LEVEL = os.getenv("REPLAY_FFMPEG_LOGLEVEL", "warning")

stop_requested = False

'''
Manages recording for the camera
'''
@dataclass
class CameraRecorder:
    camera: dict
    worker: threading.Thread | None = None
    current_process: subprocess.Popen | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    last_exit_code: int | None = None
    last_error: str = ""
    current_clip_path: Path | None = None

    @property
    def recording_id(self) -> str:
        return str(
            self.camera.get("recording_id")
            or self.camera.get("camera_site")
            or self.camera.get("id")
            or ""
        )

    @property
    def camera_site(self) -> str:
        return str(self.camera.get("camera_site") or self.recording_id)

    @property
    def clips_dir(self) -> Path:
        return replay_clips_dir(self.recording_id)

    @property
    def status_path(self) -> Path:
        return replay_camera_dir(self.recording_id) / "status.json"

    @property
    def log_path(self) -> Path:
        return replay_camera_dir(self.recording_id) / "ffmpeg.log"

    # writes the recorder’s current state to disk
    def write_status(self, state: str, extra: dict | None = None) -> None:
        payload = {
            "recording_id": self.recording_id,
            "camera_site": self.camera_site,
            "camera_id": str(self.camera.get("id") or ""),
            "camera_name": str(self.camera.get("name") or self.recording_id),
            "stream_url": str(self.camera.get("stream_url") or ""),
            "state": state,
            "pid": self.current_process.pid if self.current_process and self.current_process.poll() is None else None,
            "segment_seconds": REPLAY_SEGMENT_SECONDS,
            "window_hours": REPLAY_WINDOW_HOURS,
            "updated_at": isoformat_z(datetime.now(UTC)),
            "last_exit_code": self.last_exit_code,
            "last_error": self.last_error,
            "current_clip_path": str(self.current_clip_path.relative_to(replay_camera_dir(self.recording_id)))
            if self.current_clip_path and self.current_clip_path.exists()
            else "",
        }
        if extra:
            payload.update(extra)

        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.status_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(self.status_path)

    # append the log line
    def append_log(self, message: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now().isoformat()}] {message}\n")

    # determines where a clip should be saved
    def clip_path_for_start(self, started_at: datetime) -> Path:
        stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
        return (
            self.clips_dir
            / started_at.strftime("%Y")
            / started_at.strftime("%m")
            / started_at.strftime("%d")
            / f"{self.recording_id}_{stamp}.mp4"
        )

    # builds the full ffmpeg command, more info https://ffmpeg.org/ffmpeg.html
    def ffmpeg_command(self, output_path: Path) -> list[str]:
        return [
            FFMPEG_BIN,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            LOG_LEVEL,
            "-y",
            "-rw_timeout",
            "15000000",
            "-fflags",
            "+genpts",
            "-i",
            str(self.camera["stream_url"]),
            "-map",
            "0:v:0",
            "-an",
            "-c",
            "copy",
            "-t",
            str(REPLAY_SEGMENT_SECONDS),
            "-movflags",
            "+faststart",
            "-avoid_negative_ts",
            "make_zero",
            str(output_path),
        ]

    # records the mp4 segment
    def capture_one_clip(self) -> bool:
        started_at = datetime.now(UTC).replace(microsecond=0)
        final_path = self.clip_path_for_start(started_at)
        temp_path = final_path.with_suffix(".part.mp4")
        temp_path.parent.mkdir(parents=True, exist_ok=True)

        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass

        command = self.ffmpeg_command(temp_path)
        self.current_clip_path = temp_path
        self.write_status(
            "capturing",
            {
                "clip_started_at": isoformat_z(started_at),
                "target_clip_path": str(final_path.relative_to(replay_camera_dir(self.recording_id))),
            },
        )
        self.append_log(f"Starting {REPLAY_SEGMENT_SECONDS}-second capture -> {final_path}")

        with self.log_path.open("a", encoding="utf-8") as log_handle:
            self.current_process = subprocess.Popen(
                command,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                cwd=str(replay_camera_dir(self.recording_id).parent),
            )
            exit_code = self.current_process.wait()

        self.current_process = None
        self.last_exit_code = exit_code

        if stop_requested or self.stop_event.is_set():
            self.last_error = ""
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            return False

        if exit_code != 0:
            self.last_error = f"ffmpeg exited with code {exit_code}"
            self.write_status("error", {"clip_started_at": isoformat_z(started_at)})
            self.append_log(self.last_error)
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            return False

        if not temp_path.exists():
            self.last_error = "ffmpeg finished but no MP4 clip was created"
            self.write_status("error", {"clip_started_at": isoformat_z(started_at)})
            self.append_log(self.last_error)
            return False

        clip_size = temp_path.stat().st_size
        if clip_size < MIN_CLIP_BYTES:
            self.last_error = f"discarded tiny clip ({clip_size} bytes)"
            self.write_status(
                "error",
                {"clip_started_at": isoformat_z(started_at), "clip_size_bytes": clip_size},
            )
            self.append_log(self.last_error)
            try:
                temp_path.unlink()
            except OSError:
                pass
            return False

        temp_path.replace(final_path)
        self.current_clip_path = final_path
        self.last_error = ""

        finished_at = datetime.now(UTC)
        self.write_status(
            "running",
            {
                "clip_started_at": isoformat_z(started_at),
                "clip_finished_at": isoformat_z(finished_at),
                "last_clip_path": str(final_path.relative_to(replay_camera_dir(self.recording_id))),
                "last_clip_size_bytes": clip_size,
            },
        )
        self.append_log(f"Saved clip {final_path} ({clip_size} bytes)")
        return True

    # removes expired clips
    def delete_old_clips(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(hours=REPLAY_WINDOW_HOURS)
        for clip_path in self.clips_dir.rglob("*.mp4"):
            start_at = parse_clip_start_from_name(clip_path.name)
            if start_at is None:
                start_at = datetime.fromtimestamp(clip_path.stat().st_mtime, UTC)
            if start_at >= cutoff:
                continue

            try:
                clip_path.unlink()
                self.append_log(f"Deleted old clip {clip_path}")
            except OSError:
                continue

        self.cleanup_empty_dirs()

    # removes empty directories
    def cleanup_empty_dirs(self) -> None:
        if not self.clips_dir.exists():
            return

        all_dirs = sorted(
            [path for path in self.clips_dir.rglob("*") if path.is_dir()],
            key=lambda item: len(item.parts),
            reverse=True,
        )
        for directory in all_dirs:
            try:
                next(directory.iterdir())
            except StopIteration:
                try:
                    directory.rmdir()
                except OSError:
                    pass
            except OSError:
                continue

    # starts the main recorder loop for the camera
    def run_forever(self) -> None:
        self.write_status("starting")
        self.append_log("Recorder thread started")

        while not stop_requested and not self.stop_event.is_set():
            success = self.capture_one_clip()
            self.delete_old_clips()

            if stop_requested or self.stop_event.is_set():
                break

            if success:
                self.write_status("sleeping")
                self.stop_event.wait(0.5)
            else:
                self.write_status("error")
                self.stop_event.wait(RESTART_DELAY_SECONDS)

        self.write_status("stopped")
        self.append_log("Recorder thread stopped")

    def start(self) -> None:
        self.stop_event.clear()
        self.worker = threading.Thread(
            target=self.run_forever,
            name=f"recorder-{self.recording_id}",
            daemon=True,
        )
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        process = self.current_process
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=15)

        self.write_status("stopped")


# a helper that extracts a UTC timestamp from a clip filename
def parse_clip_start_from_name(filename: str) -> datetime | None:
    stem = Path(filename).name
    try:
        timestamp_part = stem.rsplit("_", 1)[1].replace(".mp4", "")
        return datetime.strptime(timestamp_part, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except Exception:
        return None


# verifies ffmpeg exists
def ensure_ffmpeg_available() -> None:
    try:
        completed = subprocess.run(
            [FFMPEG_BIN, "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as error:
        raise SystemExit(
            f"Could not find ffmpeg binary '{FFMPEG_BIN}'. Install ffmpeg or set FFMPEG_BIN."
        ) from error

    if completed.returncode != 0:
        raise SystemExit(f"ffmpeg exists but could not be started (exit {completed.returncode}).")


def install_signal_handlers() -> None:
    def _handle_signal(_signum, _frame):
        global stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

# loads the desired cameras into the dictionary
def load_target_cameras() -> dict[str, dict]:
    cameras_by_recording_id: Dict[str, dict] = {}

    for camera in load_managed_cameras():
        if not isinstance(camera, dict):
            continue

        recording_id = str(
            camera.get("recording_id")
            or camera.get("camera_site")
            or camera.get("id")
            or ""
        ).strip()
        stream_url = str(camera.get("stream_url") or "").strip()
        if not recording_id or not stream_url:
            continue

        normalized = dict(camera)
        normalized["recording_id"] = recording_id
        cameras_by_recording_id[recording_id] = normalized

    return cameras_by_recording_id


def sync_recorders(recorders: Dict[str, CameraRecorder]) -> Dict[str, CameraRecorder]:
    target_cameras = load_target_cameras()

    for recording_id, camera in target_cameras.items():
        existing = recorders.get(recording_id)
        if existing is None:
            recorder = CameraRecorder(camera)
            recorder.start()
            recorders[recording_id] = recorder
            print(f"Started recorder for {recording_id}")
            continue

        if str(existing.camera.get("stream_url") or "").strip() != str(camera.get("stream_url") or "").strip():
            print(f"Restarting recorder for {recording_id} because the stream URL changed")
            existing.stop()
            recorder = CameraRecorder(camera)
            recorder.start()
            recorders[recording_id] = recorder
            continue

        existing.camera = camera

    for recording_id in list(recorders.keys()):
        if recording_id in target_cameras:
            continue

        print(f"Stopping recorder for removed camera {recording_id}")
        recorders[recording_id].stop()
        del recorders[recording_id]

    return recorders


def main() -> int:
    ensure_ffmpeg_available()
    install_signal_handlers()

    print("Starting local MP4 recorder manager.")
    print(
        f"Watching managed_cameras.json every {REPLAY_MONITOR_INTERVAL_SECONDS} seconds. "
        f"Each assigned camera reconnects every {REPLAY_SEGMENT_SECONDS} seconds and writes MP4 clips under the recordings directory."
    )

    recorders: Dict[str, CameraRecorder] = {}
    last_empty_notice_at = 0.0

    try:
        while not stop_requested:
            sync_recorders(recorders)

            if not recorders:
                now = time.monotonic()
                if now - last_empty_notice_at >= max(5, REPLAY_MONITOR_INTERVAL_SECONDS):
                    print(
                        "No managed cameras are assigned yet. Add one from the admin page and recording will start."
                    )
                    last_empty_notice_at = now

            time.sleep(REPLAY_MONITOR_INTERVAL_SECONDS)
    finally:
        print("Stopping local MP4 recorder manager...")
        for recorder in list(recorders.values()):
            recorder.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
