from pathlib import Path
from types import SimpleNamespace

import pytest

from hindsight_api import pg0


def test_pg0_installation_root_uses_user_pg0_home(monkeypatch, tmp_path):
    monkeypatch.setattr(pg0.Path, "home", lambda: tmp_path)

    assert pg0._pg0_installation_root("18.1.0") == (
        tmp_path / ".pg0" / "installation" / "18.1.0"
    )


def test_pgvector_probe_success_skips_rebuild(monkeypatch, tmp_path):
    install_root = tmp_path / ".pg0" / "installation" / "18.1.0"
    bin_dir = install_root / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / pg0._executable_name("psql")).write_text("", encoding="utf-8")
    monkeypatch.setattr(pg0.Path, "home", lambda: tmp_path)

    probes = []
    monkeypatch.setattr(
        pg0,
        "_probe_pgvector_extension",
        lambda root, uri: probes.append((root, uri)),
    )
    monkeypatch.setattr(
        pg0,
        "_rebuild_pgvector_for_pg0",
        lambda _root: pytest.fail("rebuild should not run"),
    )

    pg0._ensure_pg0_pgvector_loadable(
        "postgresql://hindsight:hindsight@127.0.0.1:5432/hindsight",
        "18.1.0",
    )

    assert probes == [
        (
            install_root,
            "postgresql://hindsight:hindsight@127.0.0.1:5432/hindsight",
        )
    ]


def test_pgvector_probe_failure_rebuilds_and_rechecks(monkeypatch, tmp_path):
    install_root = tmp_path / ".pg0" / "installation" / "18.1.0"
    bin_dir = install_root / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / pg0._executable_name("psql")).write_text("", encoding="utf-8")
    monkeypatch.setattr(pg0.Path, "home", lambda: tmp_path)

    calls = []

    def fake_probe(root, uri):
        calls.append(("probe", root, uri))
        if len([call for call in calls if call[0] == "probe"]) == 1:
            raise RuntimeError("GLIBC_2.38 not found")

    monkeypatch.setattr(pg0, "_probe_pgvector_extension", fake_probe)
    monkeypatch.setattr(
        pg0,
        "_rebuild_pgvector_for_pg0",
        lambda root: calls.append(("rebuild", root)),
    )

    pg0._ensure_pg0_pgvector_loadable(
        "postgresql://hindsight:hindsight@127.0.0.1:5432/hindsight",
        "18.1.0",
    )

    assert calls == [
        (
            "probe",
            install_root,
            "postgresql://hindsight:hindsight@127.0.0.1:5432/hindsight",
        ),
        ("rebuild", install_root),
        (
            "probe",
            install_root,
            "postgresql://hindsight:hindsight@127.0.0.1:5432/hindsight",
        ),
    ]


def test_pgvector_rebuild_uses_pg0_pg_config(monkeypatch, tmp_path):
    install_root = tmp_path / ".pg0" / "installation" / "18.1.0"
    bin_dir = install_root / "bin"
    bin_dir.mkdir(parents=True)
    pg_config = bin_dir / pg0._executable_name("pg_config")
    pg_config.write_text("", encoding="utf-8")
    source_dir = tmp_path / "pgvector-0.8.1"
    source_dir.mkdir()
    monkeypatch.setattr(pg0.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        pg0,
        "_download_pgvector_source",
        lambda version, temp_path: source_dir,
    )

    calls = []

    def fake_run_make(path, *args, env, check=True):
        calls.append((path, args, env["PG_CONFIG"], check))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pg0, "_run_make", fake_run_make)

    pg0._rebuild_pgvector_for_pg0(install_root)

    assert calls == [
        (source_dir, ("clean",), str(pg_config), False),
        (source_dir, (), str(pg_config), True),
        (source_dir, ("install",), str(pg_config), True),
    ]
