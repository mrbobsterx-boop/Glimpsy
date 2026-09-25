"""Переезд Worklapse → Glimpsy: настройки, проекты и автозапуск переносятся, ничего не теряется."""

import json

from glimpsy import migrate


def test_migrate_moves_everything(tmp_path, monkeypatch):
    old_cfg, old_data = tmp_path / "cfg" / "Worklapse", tmp_path / "data" / "Worklapse"
    new_cfg, new_data = tmp_path / "cfg" / "Glimpsy", tmp_path / "data" / "Glimpsy"
    (old_cfg).mkdir(parents=True)
    (old_cfg / "settings.json").write_text(json.dumps({"output_dir": "/videos/Worklapse"}))
    (old_data / "projects" / "project_1").mkdir(parents=True)
    (old_data / "projects" / "project_1" / "project.json").write_text("{}")
    (old_data / "models").mkdir()
    (old_data / "models" / "ggml-base.bin").write_bytes(b"model")
    # в новой папке уже есть свой проект — он остаётся, старые добавляются рядом
    (new_data / "projects" / "project_2").mkdir(parents=True)
    old_auto = tmp_path / "xdg" / "autostart" / "worklapse.desktop"
    old_auto.parent.mkdir(parents=True)
    old_auto.write_text("[Desktop Entry]")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(migrate, "user_config_dir", lambda name, appauthor=False: str(tmp_path / "cfg" / name))
    monkeypatch.setattr(migrate, "user_data_dir", lambda name, appauthor=False: str(tmp_path / "data" / name))
    monkeypatch.setattr(migrate.paths, "config_dir", lambda: new_cfg)
    monkeypatch.setattr(migrate.paths, "data_dir", lambda: new_data)
    monkeypatch.setattr(migrate.paths, "temp_root", lambda: tmp_path / "tmp" / "Glimpsy")
    monkeypatch.setattr(migrate.sys, "platform", "linux")
    enabled = []
    monkeypatch.setattr(migrate.autostart, "set_enabled", lambda on: enabled.append(on))

    assert migrate.run() >= 3
    assert json.loads((new_cfg / "settings.json").read_text())["output_dir"] == "/videos/Worklapse"
    assert (new_data / "projects" / "project_1" / "project.json").exists()
    assert (new_data / "projects" / "project_2").exists()
    assert (new_data / "models" / "ggml-base.bin").read_bytes() == b"model"
    assert not old_cfg.exists() and not old_auto.exists() and enabled == [True]
    assert migrate.run() == 0                              # второй запуск ничего не делает
