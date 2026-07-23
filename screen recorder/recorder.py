"""
Vellum Screen Capture
Recorder Engine (FFmpeg wrapper)
"""

import os
import signal
import subprocess
import platform
from datetime import datetime

from config import RECORDINGS_DIR
from audio_manager import AudioManager


class Recorder:

    def __init__(self, ffmpeg_manager, settings):
        self.ffmpeg = ffmpeg_manager
        self.settings = settings
        self.audio = AudioManager()
        self.process = None
        self.output_file = None
        self.last_error = None

    # ----------------------------------------
    # Build output filename
    # ----------------------------------------

    def build_output_path(self):
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        fmt = self.settings.get("format", "mp4")
        return str(RECORDINGS_DIR / f"recording_{timestamp}.{fmt}")

    # ----------------------------------------
    # Detect display server (Linux)
    # ----------------------------------------

    @staticmethod
    def _is_wayland():
        return (
            os.environ.get("WAYLAND_DISPLAY") is not None
            or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        )

    # ----------------------------------------
    # Build FFmpeg command
    # ----------------------------------------

    def build_command(self):
        system = platform.system()
        quality = self.settings.get("quality", "High")
        fps = self.settings.get("fps", 60)
        use_hw = self.settings.get("hardware_encoder", True)

        # --- Screen input ---
        if system == "Windows":
            screen_input = [
                "-f", "gdigrab",
                "-framerate", str(fps),
                "-i", "desktop",
            ]

        elif system == "Darwin":
            screen_input = [
                "-f", "avfoundation",
                "-framerate", str(fps),
                "-i", "1:none",
            ]

        else:  # Linux
            if self._is_wayland():
                screen_input = [
                    "-f", "pipewire",
                    "-framerate", str(fps),
                    "-i", "0",
                ]
            else:
                display = os.environ.get("DISPLAY", ":0.0")
                screen_input = [
                    "-f", "x11grab",
                    "-framerate", str(fps),
                    "-i", display,
                ]

        # --- Audio inputs ---
        audio_inputs = []
        if self.settings.get("microphone", False):
            audio_inputs += self.audio.get_mic_input()
        if self.settings.get("system_audio", False):
            audio_inputs += self.audio.get_system_audio_input()

        # Mix multiple audio streams into one track when both are active
        audio_filter = []
        num_audio = sum([
            bool(self.settings.get("microphone", False)),
            bool(self.settings.get("system_audio", False)),
        ])
        if num_audio > 1:
            audio_filter = [
                "-filter_complex", f"amix=inputs={num_audio}:duration=first",
            ]

        # --- Encoder & quality ---
        encoder = self.ffmpeg.get_best_encoder() if use_hw else "libx264"

        quality_map = {
            "Ultra":  ("ultrafast", "18"),
            "High":   ("veryfast",  "23"),
            "Medium": ("fast",      "28"),
            "Low":    ("ultrafast", "32"),
        }

        if encoder == "libx264":
            preset, crf = quality_map.get(quality, ("veryfast", "23"))
            video_settings = [
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", crf,
                "-pix_fmt", "yuv420p",  # required for broad player compatibility
            ]

        elif encoder == "h264_vaapi":
            qp_map = {"Ultra": "18", "High": "23", "Medium": "28", "Low": "34"}
            video_settings = [
                "-vaapi_device", self.ffmpeg.vaapi_device,
                "-vf", "format=nv12,hwupload",
                "-c:v", "h264_vaapi",
                "-qp", qp_map.get(quality, "23"),
                # no -pix_fmt: hwupload sets it
            ]

        elif encoder in ("h264_nvenc", "hevc_nvenc"):
            cq_map = {"Ultra": "18", "High": "23", "Medium": "28", "Low": "34"}
            video_settings = [
                "-c:v", encoder,
                "-preset", "p4",
                "-rc", "vbr",
                "-cq", cq_map.get(quality, "23"),
                "-pix_fmt", "yuv420p",
            ]

        elif encoder == "h264_amf":
            video_settings = [
                "-c:v", "h264_amf",
                "-quality", "balanced",
                "-pix_fmt", "yuv420p",
            ]

        else:
            video_settings = ["-c:v", encoder, "-pix_fmt", "yuv420p"]

        return [
            self.ffmpeg.ffmpeg_path,
            "-y",              # overwrite output without asking
            *screen_input,
            *audio_inputs,
            *audio_filter,
            *video_settings,
            self.output_file,
        ]

    # ----------------------------------------
    # Start recording
    # ----------------------------------------

    def start(self):
        self.last_error = None

        if not self.ffmpeg.is_installed():
            self.last_error = "ffmpeg not found (not on PATH and not bundled next to the app)"
            return

        self.output_file = self.build_output_path()
        cmd = self.build_command()

        # On Windows, ffmpeg needs to be in its own process group so we can
        # later send it CTRL_BREAK_EVENT (the closest equivalent to SIGINT)
        # instead of hard-killing it. Without this flag there is no way to
        # ask ffmpeg to finalize the file cleanly - taskkill /F terminates
        # it mid-write, corrupting the recording.
        creationflags = 0
        if platform.system() == "Windows":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,   # don't use stdin; we stop via signal
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,     # capture for error reporting
                creationflags=creationflags,
            )
        except Exception as e:
            self.process = None
            self.last_error = f"Failed to launch ffmpeg: {e}"

    # ----------------------------------------
    # Stop recording
    # ----------------------------------------

    def is_recording(self):
        return self.process is not None

    def stop(self):
        if not self.process:
            # Recording never actually started (e.g. ffmpeg missing or
            # failed to launch) - surface the real reason instead of
            # silently returning nothing.
            return None, self.last_error or "Recording never started"

        try:
            # Ask ffmpeg to finalize the container cleanly rather than
            # hard-killing it. A forceful kill (taskkill /F, terminate())
            # cuts ffmpeg off mid-write and leaves the output file with no
            # valid index/moov atom - the file is left corrupted even
            # though the app reports success.
            if platform.system() == "Windows":
                # CTRL_BREAK_EVENT is Windows' closest equivalent to SIGINT
                # for a subprocess. Requires the process to have been
                # started with CREATE_NEW_PROCESS_GROUP (see start()).
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.process.send_signal(signal.SIGINT)

            _, stderr_bytes = self.process.communicate(timeout=10)

            # 255 / -2 are normal ffmpeg exit codes after SIGINT on
            # POSIX; Windows returns 0 (sometimes 3221225786, the
            # STATUS_CONTROL_C_EXIT code) after a clean CTRL_BREAK_EVENT.
            if self.process.returncode not in (0, 255, -2, 3221225786):
                self.last_error = stderr_bytes.decode(errors="replace").strip()

        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            self.last_error = "ffmpeg did not stop in time and was force-killed"

        except Exception as e:
            self.last_error = str(e)

        finally:
            self.process = None

        return self.output_file, self.last_error
