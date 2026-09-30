"""会话记录开关：默认读 .env，运行时可经 API 覆盖。"""

from __future__ import annotations

from grannyflat_bot.config import get_settings


class RecordingState:
    def __init__(self) -> None:
        self._runtime_override: bool | None = None

    @property
    def enabled(self) -> bool:
        if self._runtime_override is not None:
            return self._runtime_override
        return get_settings().session_recording_enabled

    def set_enabled(self, value: bool) -> None:
        self._runtime_override = value

    def clear_override(self) -> None:
        self._runtime_override = None

    def status(self) -> dict[str, bool | str | None]:
        settings = get_settings()
        return {
            "enabled": self.enabled,
            "env_default": settings.session_recording_enabled,
            "runtime_override": self._runtime_override,
            "export_dir": str(settings.session_export_dir),
        }


_recording_state = RecordingState()


def get_recording_state() -> RecordingState:
    return _recording_state
