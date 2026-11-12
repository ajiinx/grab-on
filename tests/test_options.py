"""Tests for format curation, using synthetic yt-dlp format dicts."""

from app.downloader import _ProgressTracker, build_ydl_opts
from app.errors import friendly_error
from app.extractor import build_options, short_side
from app.models import DownloadOption
from pathlib import Path

DURATION = 600  # seconds


def vfmt(fid, w, h, vcodec="avc1.640028", fps=30, size=None, proto="https", tbr=1000, acodec="none", ext="mp4"):
    return {
        "format_id": fid, "url": "https://x/" + fid, "ext": ext, "width": w, "height": h,
        "vcodec": vcodec, "acodec": acodec, "fps": fps, "filesize": size, "protocol": proto, "tbr": tbr,
    }


def afmt(fid, ext="m4a", acodec="mp4a.40.2", abr=128, size=None, proto="https", lang=-1, note=""):
    return {
        "format_id": fid, "url": "https://x/" + fid, "ext": ext, "vcodec": "none", "acodec": acodec,
        "abr": abr, "tbr": abr, "filesize": size, "protocol": proto, "language_preference": lang, "format_note": note,
    }


def youtube_info(formats):
    return {"id": "abc", "title": "t", "duration": DURATION, "formats": formats}


FULL_4K = [
    vfmt("160", 256, 144),
    vfmt("134", 640, 360),
    vfmt("135", 854, 480, size=27_000_000),
    vfmt("244", 854, 480, vcodec="vp9", size=31_000_000, ext="webm"),
    vfmt("298", 1280, 720, fps=60, size=143_000_000),
    vfmt("398", 1280, 720, vcodec="av01.0.08M.08", fps=60, size=68_000_000),
    vfmt("299", 1920, 1080, fps=60, size=245_000_000),
    vfmt("312", 1920, 1080, fps=60, proto="m3u8_native", tbr=7400),
    vfmt("303", 1920, 1080, vcodec="vp9", fps=60, size=160_000_000, ext="webm"),
    vfmt("308", 2560, 1440, vcodec="vp9", fps=60, size=451_000_000, ext="webm"),
    vfmt("315", 3840, 2160, vcodec="vp9", fps=60, size=1_270_000_000, ext="webm"),
    vfmt("401", 3840, 2160, vcodec="av01.0.13M.08", fps=60, size=679_000_000),
    afmt("140", size=9_800_000),
    afmt("140-drc", size=9_800_000, note="medium, DRC"),
    afmt("251", ext="webm", acodec="opus", abr=135, size=9_700_000),
    {"format_id": "233", "url": "https://x/233", "ext": "mp4", "vcodec": "none", "acodec": None, "protocol": "m3u8_native"},
    {"format_id": "sb0", "url": "https://x/sb", "ext": "mhtml", "vcodec": "none", "acodec": "none"},
]


def by_id(options):
    return {o.id: o for o in options}


def test_4k_video_gets_all_tiers_in_order():
    opts = build_options(youtube_info(FULL_4K), "youtube")
    assert [o.id for o in opts] == ["best", "1080", "720", "480", "mp3", "m4a"]


def test_best_picks_highest_res_and_prefers_vp9_over_av1():
    best = by_id(build_options(youtube_info(FULL_4K), "youtube"))["best"]
    assert best.label == "2160p · 4K"
    assert best.detail.startswith("Best · MP4")
    assert best.format.startswith("315+140/")
    assert best.height == 2160


def test_tiers_prefer_h264_and_https_and_m4a_audio():
    o = by_id(build_options(youtube_info(FULL_4K), "youtube"))
    assert o["1080"].format.startswith("299+140/")  # avc over vp9, https over m3u8
    assert o["720"].format.startswith("298+140/")
    assert o["480"].format.startswith("135+140/")
    assert "bv*[height<=1080]+ba" in o["1080"].format  # generic fallback present


def test_size_estimate_sums_video_and_audio():
    o = by_id(build_options(youtube_info(FULL_4K), "youtube"))
    assert o["1080"].filesize == 245_000_000 + 9_800_000
    assert o["1080"].stream_sizes == [245_000_000, 9_800_000]


def test_size_estimate_falls_back_to_bitrate():
    info = youtube_info([vfmt("22", 1280, 720, tbr=2000, acodec="mp4a.40.2"), afmt("140", size=None, abr=128)])
    o = by_id(build_options(info, "youtube"))
    assert o["720"].filesize == 2000 * 1000 // 8 * DURATION
    assert o["m4a"].filesize == 128 * 1000 // 8 * DURATION


def test_unknown_size_gives_none():
    info = {"id": "x", "formats": [vfmt("1", 1280, 720, tbr=None), afmt("2", abr=None)]}
    o = by_id(build_options(info, "youtube"))
    assert o["720"].filesize is None


def test_only_existing_tiers_are_listed():
    info = youtube_info([vfmt("134", 640, 360), vfmt("135", 854, 480), vfmt("136", 1280, 720), afmt("140")])
    assert [o.id for o in build_options(info, "youtube")] == ["720", "480", "mp3", "m4a"]


def test_no_best_option_when_max_is_1080():
    info = youtube_info([vfmt("137", 1920, 1080), vfmt("136", 1280, 720), afmt("140")])
    ids = [o.id for o in build_options(info, "youtube")]
    assert "best" not in ids and ids[0] == "1080"


def test_low_res_only_video_falls_back_to_best():
    info = youtube_info([vfmt("160", 256, 144), vfmt("133", 426, 240), afmt("140")])
    opts = build_options(info, "youtube")
    assert opts[0].id == "best"
    assert opts[0].label == "240p"


def test_vertical_short_uses_short_side():
    info = youtube_info([vfmt("137", 1080, 1920), vfmt("136", 720, 1280), afmt("140")])
    o = by_id(build_options(info, "youtube"))
    assert set(o) == {"1080", "720", "mp3", "m4a"}
    assert "[width<=1080]" in o["1080"].format  # fallback filters on width for portrait
    assert short_side({"width": 1080, "height": 1920}) == 1080


def test_drc_and_hls_audio_are_skipped():
    o = by_id(build_options(youtube_info(FULL_4K), "youtube"))
    assert "drc" not in o["1080"].format
    assert "233" not in o["1080"].format


def test_original_language_audio_preferred_over_dub():
    info = youtube_info([
        vfmt("137", 1920, 1080),
        afmt("140-1", lang=-1, abr=160),  # dub, higher bitrate
        afmt("140-0", lang=10, abr=128),  # original
    ])
    o = by_id(build_options(info, "youtube"))
    assert o["1080"].format.startswith("137+140-0/")
    assert o["m4a"].format.startswith("140-0/")


def test_mp3_uses_best_audio_any_codec():
    o = by_id(build_options(youtube_info(FULL_4K), "youtube"))
    assert o["mp3"].format.startswith("251/")  # opus 135k beats m4a 128k as a source
    assert o["mp3"].ext == "mp3"


def test_no_m4a_option_without_m4a_audio():
    info = youtube_info([vfmt("137", 1920, 1080), afmt("251", ext="webm", acodec="opus")])
    assert "m4a" not in by_id(build_options(info, "youtube"))


def test_progressive_only_video_no_merge():
    info = youtube_info([vfmt("18", 640, 360, acodec="mp4a.40.2"), vfmt("22", 1280, 720, acodec="mp4a.40.2")])
    o = by_id(build_options(info, "youtube"))
    assert o["720"].format.startswith("22/")
    assert "+" not in o["720"].format.split("/")[0]
    assert "mp3" in o  # audio can be extracted from progressive files


def test_audio_only_media():
    info = youtube_info([afmt("140"), afmt("251", ext="webm", acodec="opus")])
    assert [o.id for o in build_options(info, "youtube")] == ["mp3", "m4a"]


def test_instagram_gets_simple_video_plus_audio():
    info = {
        "id": "C1", "duration": 30, "formats": [
            vfmt("dash-1v", 720, 1280, tbr=1000),
            vfmt("dash-2v", 480, 854, tbr=500),
            afmt("dash-a", abr=96),
        ],
    }
    opts = build_options(info, "instagram")
    assert [o.id for o in opts] == ["best", "mp3", "m4a"]
    assert opts[0].label == "Video" and opts[0].detail == "MP4 · 720p"
    assert opts[0].format.startswith("dash-1v+dash-a/")


def test_instagram_silent_video_has_no_audio_options():
    info = {"id": "C1", "formats": [vfmt("dash-1v", 720, 1280)]}
    assert [o.id for o in build_options(info, "instagram")] == ["best"]


def test_single_inline_format():
    info = {"id": "x", "url": "https://x/v.mp4", "ext": "mp4", "width": 1280, "height": 720,
            "vcodec": "avc1", "acodec": "mp4a", "format_id": "0", "duration": 10}
    assert [o.id for o in build_options(info, "instagram")] == ["best", "mp3"]


def test_no_formats_gives_no_options():
    assert build_options({"id": "x", "formats": []}, "youtube") == []


# --------------------------------------------------------------------------- #
# Downloader helpers
# --------------------------------------------------------------------------- #


def _opt(**kw):
    base = dict(id="720", label="720p", detail="", kind="video", ext="mp4", format="1+2")
    return DownloadOption(**(base | kw))


def pp_keys(opts):
    return [p["key"] for p in opts["postprocessors"]]


def test_ydl_opts_for_mp3():
    opts = build_ydl_opts(_opt(id="mp3", kind="audio", ext="mp3", format="251/ba"), Path("/tmp/j"), embed=False)
    assert opts["postprocessors"] == [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
    assert opts["format"] == "251/ba"
    assert opts["noplaylist"] is True


def test_ydl_opts_embed_metadata_and_cover():
    opts = build_ydl_opts(_opt(id="mp3", kind="audio", ext="mp3"), Path("/tmp/j"), embed=True)
    assert opts["writethumbnail"] is True
    # cover converted to jpg first, tags + cover written after the MP3 conversion
    assert pp_keys(opts) == ["FFmpegThumbnailsConvertor", "FFmpegExtractAudio", "FFmpegMetadata", "EmbedThumbnail"]


def test_ydl_opts_video_with_subtitles():
    opts = build_ydl_opts(_opt(), Path("/tmp/j"), subtitle="auto:en-orig", embed=False)
    assert opts["writeautomaticsub"] is True and opts["writesubtitles"] is False
    assert opts["subtitleslangs"] == ["en-orig"]
    assert pp_keys(opts) == ["FFmpegVideoRemuxer", "FFmpegEmbedSubtitle"]


def test_ydl_opts_subtitle_only():
    opts = build_ydl_opts(_opt(id="srt", kind="subtitle", ext="srt", format=""), Path("/tmp/j"), subtitle="de")
    assert opts["skip_download"] is True and opts["writesubtitles"] is True
    assert opts["subtitleslangs"] == ["de"] and "format" not in opts
    assert pp_keys(opts) == ["FFmpegSubtitlesConvertor"]


def test_ydl_opts_playlist_item_and_sort():
    opts = build_ydl_opts(_opt(format="bv*+ba/b", format_sort=["res:720", "fps"]), Path("/tmp/j"), playlist_items="3")
    assert opts["noplaylist"] is False and opts["playlist_items"] == "3"
    assert opts["format_sort"] == ["res:720", "fps"]


def test_ydl_opts_for_video_merges_to_mp4():
    opts = build_ydl_opts(_opt(), Path("/tmp/j"))
    assert opts["merge_output_format"] == "mp4/mkv"


def test_ydl_opts_for_m4a_no_reencode():
    opts = build_ydl_opts(_opt(id="m4a", kind="audio", ext="m4a"), Path("/tmp/j"), embed=False)
    assert opts["postprocessors"] == []
    opts = build_ydl_opts(_opt(id="m4a", kind="audio", ext="m4a"), Path("/tmp/j"), embed=True)
    assert "FFmpegExtractAudio" not in pp_keys(opts)


def test_subtitle_files_do_not_affect_progress():
    events = []
    t = _ProgressTracker(_opt(stream_sizes=[100]), events.append, None)
    t.progress_hook({"status": "downloading", "filename": "x.en.vtt", "downloaded_bytes": 5, "total_bytes": 10})
    assert t.files == {} and events == []


def test_progress_weights_streams_by_size():
    events = []
    t = _ProgressTracker(_opt(stream_sizes=[900, 100]), events.append, None)
    t.progress_hook({"status": "downloading", "filename": "v", "downloaded_bytes": 450, "total_bytes": 900})
    assert events[-1].percent == 45.0
    t.progress_hook({"status": "finished", "filename": "v", "downloaded_bytes": 900, "total_bytes": 900})
    t._last_emit = 0
    t.progress_hook({"status": "downloading", "filename": "a", "downloaded_bytes": 50, "total_bytes": 100})
    assert events[-1].percent == 95.0


def test_progress_without_sizes_splits_evenly():
    t = _ProgressTracker(_opt(stream_sizes=[None, None]), lambda e: None, None)
    t.files = {"v": (10, None)}
    assert t.overall_percent() == 0.0
    t.files = {"v": (100, 100), "a": (5, None)}  # audio size unknown
    assert t.overall_percent() == 50.0
    t.files = {"v": (100, 100), "a": (5, 10)}  # both known -> byte-weighted
    assert round(t.overall_percent(), 1) == 95.5


def test_friendly_errors():
    assert friendly_error(Exception("ERROR: [youtube] x: Private video. Sign in")).code == "private"
    assert friendly_error(Exception("Sign in to confirm your age")).code == "age_restricted"
    assert friendly_error(Exception("Sign in to confirm you’re not a bot")).code == "bot_check"
    assert friendly_error(Exception("Instagram sent an empty media response ... --cookies")).code == "login_required"
    assert friendly_error(Exception("Unable to download webpage: timed out")).code == "network"
    e = friendly_error(Exception("ERROR: [youtube] abc: something odd"))
    assert e.code == "unknown" and e.message == "something odd"
