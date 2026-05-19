"""Shared audio synthesis and playback utilities for tone-dear."""

import math
import os
import struct
import subprocess
import tempfile
import threading

SAMPLE_RATE = 44100
BASE_DURATION = 2.0
AMPLITUDE = 0.6

# Piano range
MIDI_LOW = 21   # A0
MIDI_HIGH = 108  # C8

# All 12 pitch classes
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Enharmonic equivalence: map every note name to its pitch class (0-11)
NAME_TO_PITCH_CLASS = {
    "C": 0, "B#": 0,
    "C#": 1, "Db": 1,
    "D": 2,
    "D#": 3, "Eb": 3,
    "E": 4, "Fb": 4,
    "F": 5, "E#": 5,
    "F#": 6, "Gb": 6,
    "G": 7,
    "G#": 8, "Ab": 8,
    "A": 9,
    "A#": 10, "Bb": 10,
    "B": 11, "Cb": 11,
}

ACCIDENTAL_CYCLE = ["", "#", "b"]


def midi_to_freq(midi_note):
    """Convert MIDI note number to frequency in Hz."""
    return 440.0 * (2.0 ** ((midi_note - 69) / 12.0))


def midi_to_name(midi_note):
    """Convert MIDI note to human-readable name like C#4."""
    pitch_class = (midi_note - 12) % 12
    octave = (midi_note - 12) // 12
    return f"{NOTE_NAMES[pitch_class]}{octave}"


def midi_to_pitch_class(midi_note):
    """Get pitch class (0-11) from MIDI note."""
    return (midi_note - 12) % 12


def loudness_compensation(freq):
    """Scale amplitude to roughly equalize perceived loudness across piano range."""
    ref = 440.0
    db_adjustment = -10 * math.log10(freq / ref)
    db_adjustment = max(-8, min(12, db_adjustment))
    return 10 ** (db_adjustment / 20)


def _duration_for_midi(midi_note):
    """Longer duration for low notes so the ear can lock on."""
    if midi_note < 36:  # below C2
        return 3.5
    elif midi_note < 48:  # below C3
        return 2.5
    return BASE_DURATION


def _build_wav(raw_bytes):
    """Wrap raw 16-bit PCM mono samples in a WAV container and write to temp file."""
    wav_header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(raw_bytes), b"WAVE", b"fmt ", 16,
        1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16,
        b"data", len(raw_bytes),
    )
    fd, path = tempfile.mkstemp(suffix=".wav")
    with os.fdopen(fd, "wb") as f:
        f.write(wav_header + raw_bytes)
    return path


def _synthesize_note(freq, duration, amplitude):
    """Synthesize a single note, return list of float samples."""
    num_samples = int(SAMPLE_RATE * duration)
    # More harmonics for low notes (missing fundamental effect)
    if freq < 100:
        harmonics = [(1, 1.0), (2, 0.6), (3, 0.4), (4, 0.3), (5, 0.2),
                     (6, 0.15), (7, 0.1), (8, 0.07)]
    else:
        harmonics = [(1, 1.0), (2, 0.5), (3, 0.25), (4, 0.12), (5, 0.06)]

    norm = sum(a for _, a in harmonics)
    comp = loudness_compensation(freq)
    # Slower decay for low notes
    decay_rate = 1.0 if freq < 100 else 1.5

    samples = []
    for i in range(num_samples):
        t = i / SAMPLE_RATE
        attack = 0.01
        if t < attack:
            envelope = t / attack
        else:
            envelope = math.exp(-decay_rate * (t - attack))

        value = 0.0
        for harmonic, amp in harmonics:
            h_envelope = envelope * math.exp(-0.5 * harmonic * t)
            value += amp * h_envelope * math.sin(2 * math.pi * freq * harmonic * t)

        value = amplitude * comp * value / norm
        samples.append(value)

    return samples


def generate_wav(midi_note):
    """Generate a WAV file for a single MIDI note."""
    freq = midi_to_freq(midi_note)
    duration = _duration_for_midi(midi_note)
    samples = _synthesize_note(freq, duration, AMPLITUDE)

    raw = b"".join(
        struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
        for s in samples
    )
    return _build_wav(raw)


def generate_chord_wav(midi_notes):
    """Generate a WAV file for a chord (multiple MIDI notes played together)."""
    if not midi_notes:
        return generate_wav(60)

    # Use the duration of the lowest note
    duration = max(_duration_for_midi(n) for n in midi_notes)
    num_samples = int(SAMPLE_RATE * duration)

    # Synthesize each note
    note_samples_list = []
    for midi_note in midi_notes:
        freq = midi_to_freq(midi_note)
        note_dur = duration
        ns = _synthesize_note(freq, note_dur, AMPLITUDE / len(midi_notes))
        # Pad if shorter
        if len(ns) < num_samples:
            ns.extend([0.0] * (num_samples - len(ns)))
        note_samples_list.append(ns)

    # Mix
    raw = b"".join(
        struct.pack("<h", int(max(-1.0, min(1.0, sum(ns[i] for ns in note_samples_list))) * 32767))
        for i in range(num_samples)
    )
    return _build_wav(raw)


def play_wav_async(path):
    """Play a WAV file asynchronously via paplay."""
    proc = subprocess.Popen(
        ["paplay", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    def cleanup():
        proc.wait()
        try:
            os.unlink(path)
        except OSError:
            pass
    threading.Thread(target=cleanup, daemon=True).start()
    return proc
