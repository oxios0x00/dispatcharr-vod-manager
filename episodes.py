"""Pure helpers for spotting a series relation whose episode list Dispatcharr
marked as fetched but only partly (or not at all) imported.

Kept free of Django imports so it can be unit-tested outside Dispatcharr."""


def count_provider_episodes(series_info):
    """Number of episodes in an Xtream get_series_info() payload.

    Mirrors how Dispatcharr's batch_process_episodes reads the 'episodes'
    key: usually a dict keyed by season number, each value a list of
    episodes, but some panels return a plain list of lists."""
    episodes = (series_info or {}).get("episodes") or {}
    seasons = episodes.values() if isinstance(episodes, dict) else episodes
    return sum(len(season) for season in seasons if isinstance(season, list))


def needs_provider_check(episode_count, sibling_max):
    """Cheap, database-only pre-filter deciding whether asking the provider
    is worth an API call: a relation with fewer episodes than another
    source of the same series is suspicious. Zero episodes is handled
    separately by the caller — it needs no confirmation."""
    return 0 < episode_count < sibling_max


def is_incomplete(episode_count, provider_count):
    """True when Dispatcharr holds fewer episodes than the provider lists."""
    return episode_count < provider_count
