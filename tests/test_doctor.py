#!/usr/bin/env python3
"""
Offline tests for the setup doctor (doctor.py). No network. Config dir and DB
path are pointed at self-managed temp locations so nothing on the real machine
is read or written.

Run with: pytest tests/test_doctor.py
"""

import sqlite3
import tempfile
from pathlib import Path

from govspend_free import db, doctor


# ------------------------------ placeholders ------------------------------

def test_looks_placeholder():
    assert doctor._looks_placeholder("")                                   # empty
    assert doctor._looks_placeholder("pat-na1-xxxxxxxx-xxxx-xxxx")         # the .example token
    assert doctor._looks_placeholder("sk-ant-...")                        # llm example
    assert doctor._looks_placeholder("your-apollo-key")
    assert not doctor._looks_placeholder("pat-na1-01H2X3realtoken4567")   # a real-looking token


# ------------------------------ config status ------------------------------

def test_config_status(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td)
        monkeypatch.setattr(doctor, "CONFIG_DIR", cfg)
        # missing (with a .example present -> hint)
        (cfg / "hubspot.yaml.example").write_text("token: pat-na1-xxxx\n", encoding="utf-8")
        status, note = doctor._config_status("hubspot.yaml", secret_key="token")
        assert status == doctor.MISSING and "copy hubspot.yaml.example" in note
        # placeholder value -> WARN
        (cfg / "hubspot.yaml").write_text("token: pat-na1-xxxxxxxx-xxxx\n", encoding="utf-8")
        assert doctor._config_status("hubspot.yaml", secret_key="token")[0] == doctor.WARN
        # real value -> OK
        (cfg / "hubspot.yaml").write_text("token: pat-na1-realtoken1234\n", encoding="utf-8")
        assert doctor._config_status("hubspot.yaml", secret_key="token")[0] == doctor.OK
        # no secret_key -> present is enough
        (cfg / "sources.yaml").write_text("arkansas: {}\n", encoding="utf-8")
        assert doctor._config_status("sources.yaml")[0] == doctor.OK


# ------------------------------ db status ------------------------------

def test_db_status_missing(monkeypatch):
    monkeypatch.setattr(doctor, "DB_PATH", Path("does-not-exist-xyz.db"))
    assert doctor._db_status()["exists"] is False


def test_db_status_counts(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        dbp = Path(td) / "test.db"
        conn = sqlite3.connect(dbp)
        conn.executescript(db._SCHEMA)
        conn.close()
        conn = sqlite3.connect(dbp)
        conn.row_factory = sqlite3.Row
        db.insert_document(conn, doc_type="federal_award", state="missouri",
                           institution="Lincoln University", title="grant", text="x",
                           categories=["Student Success & Retention"])
        db.insert_document(conn, doc_type="board_minutes", state="arkansas",
                           institution="UA", title="minutes", text="y")
        conn.commit(); conn.close()

        monkeypatch.setattr(doctor, "DB_PATH", dbp)
        st = doctor._db_status()
        assert st["exists"] and st["documents"] == 2 and st["states"] == 2
        assert st["by_type"] == {"federal_award": 1, "board_minutes": 1}


# ------------------------------ gather / format ------------------------------

def test_gather_shape_and_serializable():
    r = doctor.gather()
    assert set(r) == {"deps", "configs", "tools", "database"}
    assert all(isinstance(v, bool) for v in r["deps"].values())          # deps -> bool
    assert all(len(v) == 2 for v in r["configs"].values())               # configs -> (status, note)
    # requests is a hard dependency, so it must be present in this env.
    assert r["deps"]["requests"] is True


def test_format_report_is_readable():
    out = doctor.format_report(doctor.gather())
    assert "SETUP DOCTOR" in out and "Dependencies:" in out and "Local database:" in out


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
