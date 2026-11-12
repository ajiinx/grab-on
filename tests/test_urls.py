import pytest

from app.errors import UserFacingError
from app.extractor import validate_url

YT = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://www.youtube.com/watch?v=aqz-KE-bpKQ", ("youtube", YT)),
        ("https://youtube.com/watch?v=aqz-KE-bpKQ&t=42s&list=PL123", ("youtube", YT)),
        ("https://m.youtube.com/watch?feature=share&v=aqz-KE-bpKQ", ("youtube", YT)),
        ("https://music.youtube.com/watch?v=aqz-KE-bpKQ", ("youtube", YT)),
        ("https://youtu.be/aqz-KE-bpKQ", ("youtube", YT)),
        ("https://youtu.be/aqz-KE-bpKQ?si=abcdef", ("youtube", YT)),
        ("youtu.be/aqz-KE-bpKQ", ("youtube", YT)),  # no scheme -> https assumed
        ("  https://youtu.be/aqz-KE-bpKQ  ", ("youtube", YT)),  # whitespace trimmed
        ("https://www.youtube.com/shorts/tPEE9ZwTmy0", ("youtube", "https://www.youtube.com/shorts/tPEE9ZwTmy0")),
        ("https://youtube.com/shorts/tPEE9ZwTmy0?feature=share", ("youtube", "https://www.youtube.com/shorts/tPEE9ZwTmy0")),
        ("https://www.youtube.com/live/aqz-KE-bpKQ", ("youtube", YT)),
        ("https://www.youtube.com/embed/aqz-KE-bpKQ", ("youtube", YT)),
        ("https://WWW.YOUTUBE.COM/watch?v=aqz-KE-bpKQ", ("youtube", YT)),
        ("https://www.instagram.com/reel/Chunk8-jurw/", ("instagram", "https://www.instagram.com/reel/Chunk8-jurw/")),
        ("https://www.instagram.com/reels/Chunk8-jurw", ("instagram", "https://www.instagram.com/reel/Chunk8-jurw/")),
        ("https://instagram.com/p/BQ0eAlwhDrw/?igsh=xyz", ("instagram", "https://www.instagram.com/p/BQ0eAlwhDrw/")),
        ("https://www.instagram.com/someuser/reel/Chunk8-jurw/", ("instagram", "https://www.instagram.com/reel/Chunk8-jurw/")),
        ("https://www.instagram.com/tv/CABC123/", ("instagram", "https://www.instagram.com/tv/CABC123/")),
        ("https://twitter.com/jack/status/20", ("twitter", "https://x.com/jack/status/20")),
        ("https://x.com/jack/status/20", ("twitter", "https://x.com/jack/status/20")),
        ("https://www.twitter.com/jack/status/20", ("twitter", "https://x.com/jack/status/20")),
        ("https://m.twitter.com/jack/status/20", ("twitter", "https://x.com/jack/status/20")),
        ("https://mobile.x.com/jack/status/20", ("twitter", "https://x.com/jack/status/20")),
        ("https://x.com/jack/status/20?s=20&t=abc", ("twitter", "https://x.com/jack/status/20")),
        ("x.com/jack/status/20", ("twitter", "https://x.com/jack/status/20")),
        ("https://twitter.com/jack/statuses/20/", ("twitter", "https://x.com/jack/status/20")),
        ("https://x.com/i/web/status/1850123456789012345", ("twitter", "https://x.com/i/status/1850123456789012345")),
        ("https://twitter.com/i/status/1850123456789012345", ("twitter", "https://x.com/i/status/1850123456789012345")),
        ("https://x.com/user_123/status/1850123456789012345/video/1", ("twitter", "https://x.com/user_123/status/1850123456789012345/video/1")),
    ],
)
def test_valid_urls(raw, expected):
    assert validate_url(raw) == expected


@pytest.mark.parametrize(
    "raw, code",
    [
        ("", "invalid_url"),
        ("not a url", "invalid_url"),
        ("http://www.youtube.com/watch?v=aqz-KE-bpKQ", "invalid_url"),  # https only
        ("ftp://youtube.com/watch?v=aqz-KE-bpKQ", "invalid_url"),
        ("https://vimeo.com/12345", "unsupported_site"),
        ("https://youtube.com.evil.com/watch?v=aqz-KE-bpKQ", "unsupported_site"),
        ("https://evilyoutube.com/watch?v=aqz-KE-bpKQ", "unsupported_site"),
        ("https://user:pass@youtube.com/watch?v=aqz-KE-bpKQ", "invalid_url"),
        ("https://youtube.com:8080/watch?v=aqz-KE-bpKQ", "invalid_url"),
        ("https://www.youtube.com/watch?v=short", "invalid_url"),  # bad id
        ("https://www.youtube.com/watch", "invalid_url"),
        ("https://www.youtube.com/@somechannel", "invalid_url"),
        ("https://www.youtube.com/watch?v=aqz-KE-bpKQ;rm -rf /", "invalid_url"),
        ("https://www.instagram.com/someuser/", "invalid_url"),  # profile, not a post
        ("https://www.instagram.com/stories/someuser/123/", "invalid_url"),
        ("https://x.com/someuser", "invalid_url"),
        ("https://twitter.com/home", "invalid_url"),
        ("https://x.com/jack/status/notanid", "invalid_url"),
        ("https://twitter.com.evil.com/jack/status/20", "unsupported_site"),
        ("https://youtu.be/", "invalid_url"),
        ("https://" + "a" * 3000, "invalid_url"),
    ],
)
def test_invalid_urls(raw, code):
    with pytest.raises(UserFacingError) as exc:
        validate_url(raw)
    assert exc.value.code == code
