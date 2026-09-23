from worklapse.config import Settings, load_settings, save_settings


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
