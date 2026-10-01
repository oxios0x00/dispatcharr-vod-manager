"""Routing of .strm files into several output folders by audio language.

The `strm_profiles` setting holds one profile per line:

    name : languages : movies_folder : series_folder [: exclusif]

`languages` is a comma-separated list of ISO 639-2 codes, or `all` (alias
`*`) to match every version, including those vod-probe could not measure.
A profile matches a version when the version has at least one of its
languages.

Which profiles receive a version:
  - if any `exclusif` profile matches, only the first of those does;
  - otherwise every matching profile does (several, if they overlap).
A version matching no profile is written nowhere. Empty setting: a single
implicit `all` profile using the plain movies/series subfolder settings,
which is the behaviour from before profiles existed.

Retention (what gets pruned from Dispatcharr) is unaffected by routing, but
the languages named by profiles are added to the target languages so that
a version kept for a profile is never pruned for lacking a target language.
"""
import re

ALL_KEYWORDS = ("all", "*")
EXCLUSIVE_KEYWORD = "exclusif"
_LANG_RE = re.compile(r"^[a-z]{3}$")


class ProfileError(ValueError):
    """The strm_profiles setting is malformed; the message names the line."""


class Profile:
    __slots__ = ("name", "languages", "movies_dir", "series_dir", "exclusive")

    def __init__(self, name, languages, movies_dir, series_dir, exclusive=False):
        self.name = name
        # None means "all": matches every version.
        self.languages = None if languages is None else frozenset(languages)
        self.movies_dir = movies_dir
        self.series_dir = series_dir
        self.exclusive = exclusive

    def matches(self, version_languages):
        if self.languages is None:
            return True
        return bool(self.languages & set(version_languages or ()))

    def __repr__(self):
        langs = "all" if self.languages is None else ",".join(sorted(self.languages))
        return (
            f"Profile({self.name!r}, {langs}, {self.movies_dir!r}, {self.series_dir!r}"
            f"{', exclusif' if self.exclusive else ''})"
        )


def _check_folder(value, line_no, role):
    if not value:
        raise ProfileError(f"line {line_no}: the {role} folder name is empty")
    if value in (".", "..") or "/" in value or "\\" in value or ".." in value:
        raise ProfileError(
            f"line {line_no}: {role} folder '{value}' must be a plain folder name "
            "(no '/', '\\' or '..')"
        )


def parse_profiles(text, default_movies_dir="movies", default_series_dir="series"):
    """Parse the setting into a list of Profile, raising ProfileError with the
    offending line number on anything malformed. Never guesses."""
    profiles = []
    for line_no, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.split(":")]
        if len(fields) not in (4, 5):
            raise ProfileError(
                f"line {line_no}: expected 'name : languages : movies folder : "
                f"series folder [: {EXCLUSIVE_KEYWORD}]', got {len(fields)} field(s)"
            )
        name, langs_field, movies_dir, series_dir = fields[:4]
        exclusive = False
        if len(fields) == 5:
            if fields[4].lower() != EXCLUSIVE_KEYWORD:
                raise ProfileError(
                    f"line {line_no}: the last field must be '{EXCLUSIVE_KEYWORD}' or absent, "
                    f"got '{fields[4]}'"
                )
            exclusive = True
        if not name:
            raise ProfileError(f"line {line_no}: the profile name is empty")

        if langs_field.lower() in ALL_KEYWORDS:
            languages = None
            if exclusive:
                raise ProfileError(
                    f"line {line_no}: an 'all' profile cannot be '{EXCLUSIVE_KEYWORD}' "
                    "(it would claim every file and empty the other profiles)"
                )
        else:
            codes = [c.strip().lower() for c in langs_field.split(",") if c.strip()]
            if not codes:
                raise ProfileError(f"line {line_no}: no language given (use codes like 'ara,tur' or 'all')")
            for code in codes:
                if not _LANG_RE.match(code):
                    raise ProfileError(
                        f"line {line_no}: '{code}' is not a 3-letter ISO 639-2 language code"
                    )
            languages = codes

        _check_folder(movies_dir, line_no, "movies")
        _check_folder(series_dir, line_no, "series")
        profiles.append(Profile(name, languages, movies_dir, series_dir, exclusive))

    if not profiles:
        return [Profile("principal", None, default_movies_dir, default_series_dir)]

    seen_names, seen_dirs = {}, {}
    all_profiles = 0
    for p in profiles:
        key = p.name.lower()
        if key in seen_names:
            raise ProfileError(f"profile name '{p.name}' is used twice")
        seen_names[key] = p
        for folder in (p.movies_dir, p.series_dir):
            if folder.lower() in seen_dirs:
                raise ProfileError(
                    f"folder '{folder}' is used by both '{seen_dirs[folder.lower()]}' and '{p.name}'; "
                    "each profile needs its own folders"
                )
            seen_dirs[folder.lower()] = p.name
        if p.languages is None:
            all_profiles += 1
    if all_profiles > 1:
        raise ProfileError("more than one 'all' profile: each file would be written once per 'all' profile")
    return profiles


def assign(profiles, version_languages):
    """The profiles that receive a version with these audio languages."""
    for p in profiles:
        if p.exclusive and p.matches(version_languages):
            return [p]
    return [p for p in profiles if not p.exclusive and p.matches(version_languages)]


def retention_languages(target_languages, profiles):
    """target_languages plus every language a profile names, order kept. An
    empty target list stays empty: it means "no language filter", and adding
    profile languages would turn it into one."""
    target = list(target_languages or [])
    if not target:
        return target
    result = list(target)
    for p in profiles:
        for code in sorted(p.languages or ()):
            if code not in result:
                result.append(code)
    return result
