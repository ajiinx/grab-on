"""Translate yt-dlp's error text into short, friendly messages."""

from __future__ import annotations

import re


class UserFacingError(Exception):
    """An error whose message is safe and useful to show the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# (code, regex matched against the lower-cased yt-dlp message, friendly text)
_PATTERNS: list[tuple[str, str, str]] = [
    ("private", r"private video|this video is private|this account is private|these tweets are protected|protected tweet",
     "This video is private. Only the owner (or approved followers) can see it."),
    ("age_restricted", r"confirm your age|age[- ]restricted|inappropriate for some users",
     "This video is age-restricted. Configure cookies from a logged-in browser to download it."),
    ("bot_check", r"not a bot",
     "YouTube is asking to confirm you're not a bot. Configure cookies from a logged-in browser (see README)."),
    ("login_required", r"login required|log in|sign in|rate-limit reached|requested content is not available|cookies",
     "This content requires logging in. Configure cookies (see README) and try again."),
    ("members_only", r"members[- ]only|join this channel",
     "This video is for channel members only."),
    ("unavailable", r"video (?:is )?unavailable|has been removed|no longer available|does not exist|not available in your country|404|tweet not found|tweet (?:is )?unavailable|tweet has been deleted|user has been suspended",
     "This video is unavailable — it may have been deleted or blocked in your region."),
    ("live", r"is live|live event|premieres in|is upcoming",
     "Live streams and upcoming premieres can't be downloaded until they've finished."),
    ("network", r"unable to download webpage|timed out|getaddrinfo|connection|network is unreachable|ssl|temporary failure",
     "Network error while contacting the site. Check your connection and try again."),
    ("unsupported", r"unsupported url|no video formats found|no video could be found|there is no video in this post",
     "Couldn't find a downloadable video at that link."),
    ("ffmpeg", r"ffmpeg|ffprobe",
     "ffmpeg failed while processing the file. Make sure ffmpeg is installed and up to date."),
]


def friendly_error(exc: BaseException) -> UserFacingError:
    if isinstance(exc, UserFacingError):
        return exc
    raw = str(exc)
    text = raw.lower()
    for code, pattern, message in _PATTERNS:
        if re.search(pattern, text):
            return UserFacingError(code, message)
    # Strip yt-dlp's "ERROR: [youtube] abc123: " prefix for the fallback message.
    cleaned = re.sub(r"^(error:\s*)?(\[[^\]]+\]\s*)?([\w-]+:\s*)?", "", raw.strip(), flags=re.I)
    return UserFacingError("unknown", cleaned[:300] or "Something went wrong.")
