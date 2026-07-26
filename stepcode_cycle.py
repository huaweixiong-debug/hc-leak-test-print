"""ATEQ StepCode cycle-edge detection without hardware side effects."""

from typing import Optional


class StepCodeCycleDetector:
    """Detect one exact 65535 -> 4 transition per ATEQ test cycle."""

    IDLE_STEP_CODE = 65535
    START_STEP_CODE = 4

    def __init__(self) -> None:
        self._state = "unarmed"

    @property
    def state(self) -> str:
        return self._state

    def observe(self, step_code: Optional[int]) -> bool:
        """Return True only for an uninterrupted idle-to-fill transition."""
        if step_code is None:
            self._state = "unarmed"
            return False

        if step_code == self.IDLE_STEP_CODE:
            self._state = "armed_idle"
            return False

        if step_code == self.START_STEP_CODE:
            if self._state == "armed_idle":
                self._state = "running"
                return True
            if self._state != "running":
                self._state = "running"
            return False

        if self._state != "running":
            self._state = "unarmed"
        return False
