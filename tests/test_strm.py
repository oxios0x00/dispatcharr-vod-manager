import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strm import build_proxy_url, id_tag, plan_suffixes, remove_stale_files, sanitize_filename, write_strm_if_changed


def test_sanitize_filename_strips_invalid_characters():
    assert sanitize_filename('Greyhound: The Chase? (2020)') == "Greyhound The Chase (2020)"


def test_sanitize_filename_strips_path_separators():
    assert sanitize_filename("A/B\\C") == "ABC"


def test_sanitize_filename_collapses_whitespace():
    assert sanitize_filename("Greyhound    (2020)") == "Greyhound (2020)"


def test_sanitize_filename_never_empty():
    assert sanitize_filename('???') == "Unknown"
    assert sanitize_filename("") == "Unknown"
    assert sanitize_filename(None) == "Unknown"


def test_id_tag_prefers_tmdb():
    assert id_tag("631842", "tt1234567") == " [tmdbid-631842]"


def test_id_tag_falls_back_to_imdb():
    assert id_tag(None, "tt1234567") == " [imdbid-tt1234567]"
    assert id_tag("", "tt1234567") == " [imdbid-tt1234567]"


def test_id_tag_empty_when_neither_known():
    assert id_tag(None, None) == ""
    assert id_tag("", "") == ""


def test_build_proxy_url_pins_stream_id():
    url = build_proxy_url("http://192.168.1.94:9292", "movie", "abc-uuid", "12345")
    assert url == "http://192.168.1.94:9292/proxy/vod/movie/abc-uuid?stream_id=12345"


def test_build_proxy_url_strips_trailing_slash_on_base():
    url = build_proxy_url("http://host:9292/", "episode", "ep-uuid", "42")
    assert url == "http://host:9292/proxy/vod/episode/ep-uuid?stream_id=42"


def test_build_proxy_url_omits_query_when_no_stream_id():
    url = build_proxy_url("http://host:9292", "movie", "abc-uuid", None)
    assert url == "http://host:9292/proxy/vod/movie/abc-uuid"


def test_plan_suffixes_shows_quality_even_for_a_single_relation():
    # No ambiguity to resolve with only one file, but the quality should
    # still be visible in the name without having to open it.
    assert plan_suffixes(["2160p"]) == [" - 2160p"]


def test_plan_suffixes_single_unprobed_relation_says_so_explicitly():
    assert plan_suffixes([None]) == [" - unprobed"]


def test_plan_suffixes_empty_input_returns_empty():
    assert plan_suffixes([]) == []


def test_plan_suffixes_uses_real_quality_labels():
    assert plan_suffixes(["2160p", "1080p"]) == [" - 01 - 2160p", " - 02 - 1080p"]


def test_plan_suffixes_ranks_by_quality_not_input_order():
    # Alphabetical sort (what Emby/Jellyfin use) would put "1080p" before
    # "2160p" as plain text — the rank prefix must fix that regardless of
    # which order the relations came in.
    assert plan_suffixes(["1080p", "2160p"]) == [" - 02 - 1080p", " - 01 - 2160p"]


def test_plan_suffixes_ranks_every_known_tier_correctly():
    labels = ["sd", "2160p", "480p", "720p", "1080p"]
    assert plan_suffixes(labels) == [
        " - 05 - sd",
        " - 01 - 2160p",
        " - 04 - 480p",
        " - 03 - 720p",
        " - 02 - 1080p",
    ]


def test_plan_suffixes_falls_back_to_positional_when_quality_unknown():
    assert plan_suffixes([None, None]) == [" - 01 - v2", " - 02 - v3"]


def test_plan_suffixes_fallback_never_collides_with_a_real_label():
    # A real "v2" quality label would never happen, but a positional
    # fallback landing on a number already used as a *real* label must
    # not collide with it.
    assert plan_suffixes(["v2", None]) == [" - 01 - v2", " - 02 - v3"]


def test_plan_suffixes_mixed_known_and_unknown():
    # Known qualities always outrank an unprobed fallback, regardless of
    # its position in the input.
    assert plan_suffixes(["2160p", None, "1080p"]) == [" - 01 - 2160p", " - 03 - v2", " - 02 - 1080p"]


def test_write_strm_if_changed_creates_new_file(tmp_path):
    path = os.path.join(str(tmp_path), "sub", "movie.strm")
    result = write_strm_if_changed(path, "http://example/movie/1.mkv")
    assert result == "created"
    with open(path) as f:
        assert f.read() == "http://example/movie/1.mkv"


def test_write_strm_if_changed_is_idempotent(tmp_path):
    path = os.path.join(str(tmp_path), "movie.strm")
    write_strm_if_changed(path, "http://example/movie/1.mkv")
    result = write_strm_if_changed(path, "http://example/movie/1.mkv")
    assert result == "unchanged"


def test_write_strm_if_changed_updates_when_content_differs(tmp_path):
    path = os.path.join(str(tmp_path), "movie.strm")
    write_strm_if_changed(path, "http://example/movie/1.mkv")
    result = write_strm_if_changed(path, "http://example/movie/2.mkv")
    assert result == "updated"
    with open(path) as f:
        assert f.read() == "http://example/movie/2.mkv"


def test_remove_stale_files_deletes_file_and_empty_parent(tmp_path):
    root = str(tmp_path)
    movie_dir = os.path.join(root, "Some Movie (2020)")
    os.makedirs(movie_dir)
    path = os.path.join(movie_dir, "Some Movie (2020) - 1080p.strm")
    with open(path, "w") as f:
        f.write("http://example/movie/1.mkv")

    removed = remove_stale_files([path], stop_dir=root)

    assert removed == 1
    assert not os.path.exists(path)
    assert not os.path.exists(movie_dir)  # emptied folder pruned too
    assert os.path.exists(root)  # but never the library root itself


def test_remove_stale_files_keeps_folder_with_remaining_files(tmp_path):
    root = str(tmp_path)
    movie_dir = os.path.join(root, "Some Movie (2020)")
    os.makedirs(movie_dir)
    stale_path = os.path.join(movie_dir, "Some Movie (2020) - 1080p.strm")
    kept_path = os.path.join(movie_dir, "Some Movie (2020) - 2160p.strm")
    for p in (stale_path, kept_path):
        with open(p, "w") as f:
            f.write("x")

    removed = remove_stale_files([stale_path], stop_dir=root)

    assert removed == 1
    assert not os.path.exists(stale_path)
    assert os.path.exists(kept_path)
    assert os.path.exists(movie_dir)  # not pruned: still has a real file


def test_remove_stale_files_ignores_already_missing_files(tmp_path):
    path = os.path.join(str(tmp_path), "gone.strm")
    removed = remove_stale_files([path], stop_dir=str(tmp_path))
    assert removed == 0


def test_remove_stale_files_never_touches_a_similarly_prefixed_sibling(tmp_path):
    root = str(tmp_path)
    stop_dir = os.path.join(root, "movies")
    sibling_dir = os.path.join(root, "movies2")  # prefix-matches "movies" but isn't under it
    os.makedirs(stop_dir)
    os.makedirs(sibling_dir)
    stale_path = os.path.join(stop_dir, "Title", "Title - 1080p.strm")
    os.makedirs(os.path.dirname(stale_path))
    with open(stale_path, "w") as f:
        f.write("x")

    remove_stale_files([stale_path], stop_dir=stop_dir)

    assert os.path.exists(sibling_dir)  # untouched


def test_best_quality_first_orders_by_rank_not_by_input_order():
    from strm import best_quality_first, plan_suffixes

    # Input order is relation-id order: the older 1080p comes before the 2160p.
    relations = ["old-1080p", "new-2160p"]
    suffixes = plan_suffixes(["1080p", "2160p"])
    assert best_quality_first(relations, suffixes) == [
        ("new-2160p", " - 01 - 2160p"),
        ("old-1080p", " - 02 - 1080p"),
    ]


def test_best_quality_first_keeps_a_single_relation():
    from strm import best_quality_first

    assert best_quality_first(["only"], [" - 1080p"]) == [("only", " - 1080p")]
