from glimpsy.config import Settings, load_settings, save_settings


def test_roundtrip(tmp_path):
    p = tmp_path / "s.json"
    s = Settings(target_length_s=90, pace="dynamic")
    save_settings(s, p)
    assert load_settings(p).target_length_s == 90
    assert load_settings(p).pace == "dynamic"


def test_validate_fixes_nonsense(tmp_path):
    s = Settings(clip_min_s=6, clip_max_s=2, pace="???", buffer_s=5, important_before_s=10).validate()
    assert s.clip_max_s >= s.clip_min_s
    assert s.pace == "medium"
    assert s.buffer_s >= 16      # «важный момент» 10+5 секунд должен помещаться в буфер


def test_broken_file_does_not_crash(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    assert load_settings(p).fps == 30


def test_clean_child_environment(monkeypatch):
    """Собранная программа на Linux не должна передавать свои библиотеки FFmpeg и другим программам."""
    import sys

    from glimpsy import paths
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    env = {"LD_LIBRARY_PATH": "/tmp/_MEI/_internal", "LD_LIBRARY_PATH_ORIG": "/opt/lib"}
    paths.clean_child_environment(env)
    assert env["LD_LIBRARY_PATH"] == "/opt/lib"
    env = {"LD_LIBRARY_PATH": "/tmp/_MEI/_internal"}
    paths.clean_child_environment(env)
    assert "LD_LIBRARY_PATH" not in env


def test_error_summary():
    from glimpsy.recorder.encoder import error_summary
    err = ("[VAAPI @ 0x1] libva error: /usr/lib/dri/radeonsi_drv_video.so init failed\n"
           "/tmp/x/libstdc++.so.6: version `GLIBCXX_3.4.32' not found (required by /usr/lib/libSPIRV-Tools.so)\n"
           "Device creation failed: -5.\n")
    s = error_summary(err)
    assert "GLIBCXX_3.4.32" in s and s.startswith("[VAAPI")


def test_old_default_hotkeys_migrate():
    from glimpsy.config import Settings
    s = Settings(hotkey_important="Ctrl+Alt+S", hotkey_pause="Ctrl+Alt+P", hotkey_finish="Ctrl+Alt+E").validate()
    assert (s.hotkey_important, s.hotkey_pause, s.hotkey_finish) == ("Ctrl+Alt+1", "Ctrl+Alt+2", "Ctrl+Alt+3")
    own = Settings(hotkey_important="Ctrl+Alt+S", hotkey_pause="Ctrl+Shift+F9", hotkey_finish="Ctrl+Alt+E").validate()
    assert own.hotkey_important == "Ctrl+Alt+S"          # свои сочетания пользователя не трогаем


def test_digit_hotkeys_formats():
    from glimpsy.platform import hotkey_format as hf
    assert hf.to_pynput("Ctrl+Alt+1") == "<ctrl>+<alt>+1"
    assert hf.to_win32("Ctrl+Alt+3") == (0x2 | 0x1, ord("3"))
    assert hf.to_portal("Ctrl+Alt+2") == "CTRL+ALT+2"
