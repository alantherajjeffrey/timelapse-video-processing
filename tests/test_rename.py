"""0.8: the new name keeps what the app remembered under its old one."""
from __future__ import annotations


def test_settings_come_over_from_the_old_name(tmp_path, monkeypatch):
    from etaluma_video.engine import userdata

    monkeypatch.delenv("ETALUMA_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    old = tmp_path / "Etaluma Video Processing"
    (old / "profiles").mkdir(parents=True)
    (old / "profiles" / "Bright.json").write_text("{}", encoding="utf-8")
    (old / "settings.json").write_text("{}", encoding="utf-8")
    (old / "preview_cache").mkdir()
    (old / "preview_cache" / "big.bin").write_bytes(b"x")
    root = userdata.user_data_dir()
    assert root == tmp_path / "Timelapse Video Processing"
    assert (root / "settings.json").is_file() and (root / "profiles" / "Bright.json").is_file()
    assert not (root / "preview_cache").exists()  # caches are rebuilt, not copied
    assert (old / "settings.json").is_file()  # the old folder is left as it was
    (root / "settings.json").write_text('{"theme": "light"}', encoding="utf-8")
    userdata.user_data_dir()  # later starts never copy again
    assert "light" in (root / "settings.json").read_text(encoding="utf-8")


def test_identity_has_no_personal_name():
    import etaluma_video as app

    assert app.APP_NAME == "Timelapse Video Processing" and app.REPOSITORY_URL.endswith("/timelapse-video-processing")
    assert app.CREDITS == "Developed by BIOMIS Team, SATIE laboratory, ENS Paris-Saclay."
    assert "contributors" in app.COPYRIGHT and not hasattr(app, "AUTHOR")
