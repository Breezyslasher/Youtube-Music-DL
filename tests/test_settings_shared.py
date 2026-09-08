"""The CLI and the web app must resolve settings the same way.

They did not: the merge was a closure inside create_app, so
`python -m beetdrop` saw environment variables only. Every CLI lyrics
command ran as though the Apple and Musixmatch tokens saved on the
Settings page were not configured, and scan-lyrics --upgrade - which
asks Apple and nothing else - became a silent no-op.
"""

from beetdrop.config import Config
from beetdrop.db import Store
from beetdrop.settings import apply_stored_settings, config_with_settings

SAVED = {
    "apple_token": "mut-from-the-ui",
    "apple_storefront": "gb",
    "mxm_token": "mxm-from-the-ui",
    "lyrics_provider": "apple",
    "word_lyrics": "1",
}


def a_config(tmp_path) -> Config:
    return Config(music_root=tmp_path / "music", scratch_root=tmp_path / "s",
                  config_dir=tmp_path / "c")


class TestCliSeesSavedSettings:
    def test_tokens_reach_the_cli(self, tmp_path):
        base = a_config(tmp_path)
        Store(base.db_path).set_settings(SAVED)

        config = config_with_settings(base)
        assert config.apple_token == "mut-from-the-ui"
        assert config.musixmatch_token == "mxm-from-the-ui"
        assert config.apple_storefront == "gb"
        assert config.lyrics_provider == "apple"
        assert config.word_lyrics is True

    def test_no_database_yet_leaves_the_environment_config(self, tmp_path):
        # A fresh install: the CLI must still run, not crash.
        base = a_config(tmp_path)
        assert config_with_settings(base).apple_token == base.apple_token

    def test_cli_and_web_agree(self, tmp_path):
        """The invariant that was broken: one merge, two callers."""
        base = a_config(tmp_path)
        store = Store(base.db_path)
        store.set_settings(SAVED)

        from_cli = config_with_settings(base)
        from_web = apply_stored_settings(base, store.get_settings())
        assert from_cli == from_web


class TestMergeRules:
    def test_env_pinned_library_wins_over_a_stored_override(self, tmp_path):
        base = a_config(tmp_path)
        merged = apply_stored_settings(
            base, {"music_root": "/somewhere/else"}, music_locked=True)
        assert merged.music_root == base.music_root

    def test_stored_library_applies_when_not_pinned(self, tmp_path):
        base = a_config(tmp_path)
        merged = apply_stored_settings(
            base, {"music_root": "/somewhere/else"}, music_locked=False)
        assert str(merged.music_root) == "/somewhere/else"

    def test_junk_values_are_ignored(self, tmp_path):
        base = a_config(tmp_path)
        merged = apply_stored_settings(base, {
            "concurrency": "not a number",
            "video_max_height": "tall",
            "lyrics_provider": "spotify",     # not a real provider
        })
        assert merged.concurrency == base.concurrency
        assert merged.video_max_height == base.video_max_height
        assert merged.lyrics_provider == base.lyrics_provider

    def test_the_base_config_is_not_mutated(self, tmp_path):
        base = a_config(tmp_path)
        apply_stored_settings(base, SAVED)
        assert base.apple_token != "mut-from-the-ui"
