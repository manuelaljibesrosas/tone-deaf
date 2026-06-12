"""Persistent configuration storage using SQLite."""

import json
import os
import sqlite3

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.path.join(_SCRIPT_DIR, "settings.db")


def _get_conn():
    """Get a connection to the settings database, creating tables if needed."""
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            correct INTEGER NOT NULL,
            total INTEGER NOT NULL,
            instrument INTEGER,
            settings TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def save_setting(key, value):
    """Save a setting (value will be JSON-serialized)."""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
        conn.commit()
    finally:
        conn.close()


def load_setting(key, default=None):
    """Load a setting (returns deserialized value, or default if not found)."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        return json.loads(row[0])
    finally:
        conn.close()


# ── Session tracking ──────────────────────────────────────────────────

def save_session(mode, started_at, ended_at, correct, total, instrument, settings_dict):
    """Record a completed training session.

    mode: "note", "chord", or "key"
    started_at, ended_at: ISO 8601 timestamp strings
    correct, total: integer score
    instrument: index into INSTRUMENTS, or None for built-in synth
    settings_dict: mode-specific configuration (JSON-serialized)
    """
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO sessions
               (mode, started_at, ended_at, correct, total, instrument, settings)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                mode,
                started_at,
                ended_at,
                correct,
                total,
                instrument,
                json.dumps(settings_dict),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_sessions(mode=None, limit=20):
    """Return recent sessions as a list of dicts, newest first.

    mode: if given, only return sessions for that mode.
    limit: maximum number of rows to return.
    """
    conn = _get_conn()
    try:
        if mode is None:
            rows = conn.execute(
                "SELECT id, mode, started_at, ended_at, correct, total, "
                "instrument, settings FROM sessions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, mode, started_at, ended_at, correct, total, "
                "instrument, settings FROM sessions WHERE mode = ? "
                "ORDER BY id DESC LIMIT ?",
                (mode, limit),
            ).fetchall()
        return [
            {
                "id": r[0],
                "mode": r[1],
                "started_at": r[2],
                "ended_at": r[3],
                "correct": r[4],
                "total": r[5],
                "instrument": r[6],
                "settings": json.loads(r[7]),
            }
            for r in rows
        ]
    finally:
        conn.close()


# ── Convenience functions for ear trainer settings ────────────────────

def save_note_training_config(enabled_pcs, instrument, octave_mode=False, sequence_length=1):
    """Save note training configuration."""
    save_setting("note.enabled_pcs", list(enabled_pcs))
    save_setting("note.instrument", instrument)
    save_setting("note.octave_mode", octave_mode)
    save_setting("note.sequence_length", sequence_length)


def load_note_training_config():
    """Load note training configuration.
    Returns (enabled_pcs, instrument, octave_mode, sequence_length)."""
    pcs = load_setting("note.enabled_pcs")
    enabled_pcs = set(pcs) if pcs is not None else set(range(12))
    instrument = load_setting("note.instrument", None)
    octave_mode = load_setting("note.octave_mode", False)
    sequence_length = load_setting("note.sequence_length", 1)
    return enabled_pcs, instrument, octave_mode, sequence_length


def save_chord_training_config(enabled_chords, enabled_inversions, instrument):
    """Save chord training configuration."""
    save_setting("chord.enabled_chords", list(enabled_chords))
    save_setting("chord.enabled_inversions", list(enabled_inversions))
    save_setting("chord.instrument", instrument)


def load_chord_training_config():
    """Load chord training configuration.
    Returns (enabled_chords, enabled_inversions, instrument)."""
    chords = load_setting("chord.enabled_chords")
    inversions = load_setting("chord.enabled_inversions")
    instrument = load_setting("chord.instrument", None)

    enabled_chords = set(chords) if chords is not None else None
    enabled_inversions = set(inversions) if inversions is not None else None
    return enabled_chords, enabled_inversions, instrument


def save_key_training_config(enabled_progressions, instrument):
    """Save key training configuration."""
    save_setting("key.enabled_progressions", list(enabled_progressions))
    save_setting("key.instrument", instrument)


def load_key_training_config():
    """Load key training configuration.
    Returns (enabled_progressions, instrument)."""
    progs = load_setting("key.enabled_progressions")
    instrument = load_setting("key.instrument", None)
    enabled_progressions = set(progs) if progs is not None else None
    return enabled_progressions, instrument
