from __future__ import annotations

import importlib
import os


def _with_env_reload(env: dict, reader):
    """Run `reader(config_module)` with `env` applied and warmap.config
    reloaded to pick it up, then restore both the environment and the
    module state. config_module is a live, shared, mutable object, so the
    value `reader` extracts must be captured (as a plain value) *before*
    the restore-and-reload in `finally`, or the caller would see the
    already-undone state.
    """
    import warmap.config as config_mod

    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        importlib.reload(config_mod)
        return reader(config_mod)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(config_mod)


def test_data_dir_overridable_by_env(tmp_path):
    override = str(tmp_path / "custom-data")
    data_dir, tile_cache_dir = _with_env_reload(
        {"WARMAP_DATA_DIR": override},
        lambda m: (str(m.DATA_DIR), str(m.TILE_CACHE_DIR)),
    )
    assert data_dir == override
    assert tile_cache_dir == str((tmp_path / "custom-data") / "tiles")


def test_config_dir_overridable_by_env(tmp_path):
    override = str(tmp_path / "custom-config")
    config_dir = _with_env_reload({"WARMAP_CONFIG_DIR": override}, lambda m: str(m.CONFIG_DIR))
    assert config_dir == override


def test_ensure_data_dir_creates_tiles_subdir(tmp_path):
    override = tmp_path / "d"

    def _reader(m):
        m.ensure_data_dir()
        return m.DATA_DIR.is_dir(), m.TILE_CACHE_DIR.is_dir()

    data_ok, tiles_ok = _with_env_reload({"WARMAP_DATA_DIR": str(override)}, _reader)
    assert data_ok
    assert tiles_ok


def test_sample_csv_and_map_html_exist_on_disk():
    import warmap.config as config_mod
    assert config_mod.SAMPLE_CSV.exists()
    assert config_mod.MAP_HTML.exists()


def test_tile_user_agent_identifies_the_app():
    import warmap.config as config_mod
    assert "warmap" in config_mod.TILE_USER_AGENT


def test_default_dirs_follow_each_platform(tmp_path):
    from warmap.config import default_config_dir, default_data_dir

    env = {"LOCALAPPDATA": str(tmp_path / "Local"), "APPDATA": str(tmp_path / "Roaming"),
           "XDG_DATA_HOME": str(tmp_path / "xdg-data"), "XDG_CONFIG_HOME": str(tmp_path / "xdg-config")}
    assert default_data_dir("win32", env) == tmp_path / "Local" / "warmap"
    assert default_config_dir("win32", env) == tmp_path / "Roaming" / "warmap"
    assert default_data_dir("linux", env) == tmp_path / "xdg-data" / "warmap"
    assert default_config_dir("linux", env) == tmp_path / "xdg-config" / "warmap"
    mac = default_data_dir("darwin", env)
    assert mac.parts[-3:] == ("Library", "Application Support", "warmap")
    assert default_config_dir("darwin", env) == mac


def test_default_dirs_fall_back_to_home_without_env():
    from warmap.config import default_config_dir, default_data_dir

    assert default_data_dir("linux", {}).parts[-3:] == (".local", "share", "warmap")
    assert default_config_dir("linux", {}).parts[-2:] == (".config", "warmap")
    assert default_data_dir("win32", {}).parts[-3:] == ("AppData", "Local", "warmap")
    assert default_config_dir("win32", {}).parts[-3:] == ("AppData", "Roaming", "warmap")


def test_tile_user_agent_carries_version_and_contact():
    import warmap
    import warmap.config as config_mod
    assert warmap.__version__ in config_mod.TILE_USER_AGENT
    assert "github.com/munzzyy/warmap" in config_mod.TILE_USER_AGENT
