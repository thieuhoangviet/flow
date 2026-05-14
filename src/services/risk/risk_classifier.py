"""Classify Google/Flow generation failures into retry/cooldown decisions."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict


@dataclass(frozen=True)
class RiskDecision:
    is_risk_error: bool
    risk_code: str
    severity: str
    cooldown_seconds: int
    risk_delta: int
    reason: str
    retry_same_worker: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_LOW = RiskDecision(False, "", "none", 0, 0, "Không phải lỗi risk đã biết", True)


def classify_generation_error(error: Any) -> RiskDecision:
    """Return a cooldown decision for a Flow/Google error payload or exception."""
    text = _normalize_error_text(error)
    upper = text.upper()

    if "PUBLIC_ERROR_UNUSUAL_ACTIVITY" in upper or "UNUSUAL_ACTIVITY" in upper:
        return RiskDecision(
            True,
            "PUBLIC_ERROR_UNUSUAL_ACTIVITY",
            "critical",
            2 * 60 * 60,
            40,
            "Google đánh dấu account/IP/session có hoạt động bất thường",
            False,
        )
    if "RECAPTCHA EVALUATION FAILED" in upper or "FAILED TO OBTAIN RECAPTCHA" in upper:
        return RiskDecision(
            True,
            "RECAPTCHA_EVALUATION_FAILED",
            "critical",
            2 * 60 * 60,
            35,
            "reCAPTCHA token/context bị Google từ chối",
            False,
        )
    if "PUBLIC_ERROR_USER_THROTTLED" in upper or "USER_THROTTLED" in upper or "TOO MANY" in upper:
        return RiskDecision(
            True,
            "PUBLIC_ERROR_USER_THROTTLED",
            "medium",
            30 * 60,
            20,
            "Request quá nhanh, cần cooldown worker/account",
            False,
        )
    if "PUBLIC_ERROR_HIGH_TRAFFIC" in upper or "HIGH_TRAFFIC" in upper:
        return RiskDecision(
            True,
            "PUBLIC_ERROR_HIGH_TRAFFIC",
            "low",
            5 * 60,
            5,
            "Google báo tải cao, cooldown ngắn để tránh retry dồn dập",
            True,
        )
    if "PUBLIC_ERROR_USER_QUOTA" in upper or "USER_QUOTA" in upper or "QUOTA" in upper:
        return RiskDecision(
            True,
            "PUBLIC_ERROR_USER_QUOTA",
            "high",
            6 * 60 * 60,
            30,
            "Account/project có dấu hiệu hết quota",
            False,
        )
    if "403" in upper or "PERMISSION_DENIED" in upper:
        return RiskDecision(
            True,
            "PERMISSION_DENIED",
            "high",
            60 * 60,
            25,
            "Google từ chối quyền truy cập, nên đổi/cooldown context",
            False,
        )
    if "429" in upper:
        return RiskDecision(
            True,
            "HTTP_429",
            "medium",
            30 * 60,
            20,
            "Rate limit 429",
            False,
        )
    return _LOW


def _normalize_error_text(error: Any) -> str:
    if error is None:
        return ""
    if isinstance(error, str):
        return error
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {error}"
    if isinstance(error, dict):
        parts = []
        for key in ("message", "detail", "code", "error", "reason"):
            value = error.get(key)
            if value:
                parts.append(str(value))
        if not parts:
            parts.append(str(error))
        return " | ".join(parts)
    return str(error)
