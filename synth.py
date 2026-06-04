"""Shared audio synthesis and playback utilities for tone-deaf."""

import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.request

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

# ── Instruments (GM program, name, MIDI low, MIDI high) ───────────────

INSTRUMENTS = [
    (0,  "Piano",      21, 108),  # A0–C8
    (24, "Guitar",     40,  88),  # E2–E6
    (40, "Violin",     55, 105),  # G3–A7
    (42, "Cello",      36,  76),  # C2–E5
    (56, "Trumpet",    54,  86),  # F#3–D6
    (65, "Alto Sax",   49,  80),  # Db3–Ab5
    (73, "Flute",      60,  98),  # C4–D7
    (11, "Vibraphone", 53,  89),  # F3–F6
]

INSTRUMENT_NAMES = [name for _, name, _, _ in INSTRUMENTS]


def instrument_range(idx):
    """Return (midi_low, midi_high) for an instrument index, or full piano range if None."""
    if idx is None:
        return (MIDI_LOW, MIDI_HIGH)
    _, _, lo, hi = INSTRUMENTS[idx]
    return (lo, hi)

# ── SoundFont discovery ───────────────────────────────────────────────

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SOUNDFONT_SEARCH_PATHS = [
    os.path.join(_SCRIPT_DIR, "soundfonts"),
    "/usr/share/sounds/sf2",
    "/usr/share/soundfonts",
    os.path.expanduser("~/.local/share/soundfonts"),
]

# Also check brew's fluidsynth share
_brew_sf2 = shutil.which("fluidsynth")
if _brew_sf2:
    _brew_prefix = os.path.dirname(os.path.dirname(os.path.realpath(_brew_sf2)))
    _SOUNDFONT_SEARCH_PATHS.append(os.path.join(_brew_prefix, "share", "fluid-synth", "sf2"))


def _find_soundfont():
    """Search for a .sf2 file in known locations."""
    for d in _SOUNDFONT_SEARCH_PATHS:
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".sf2") and not f.startswith("."):
                return os.path.join(d, f)
    return None


_SOUNDFONT_URL = (
    "https://github.com/mrbumpy409/GeneralUser-GS/raw/main/GeneralUser-GS.sf2"
)
_SOUNDFONT_FILENAME = "GeneralUser-GS.sf2"


def _download_soundfont():
    """Download GeneralUser-GS SoundFont to the local soundfonts directory.
    Returns the path on success, None on failure."""
    dest_dir = os.path.join(_SCRIPT_DIR, "soundfonts")
    dest_path = os.path.join(dest_dir, _SOUNDFONT_FILENAME)
    tmp_path = dest_path + ".downloading"

    try:
        os.makedirs(dest_dir, exist_ok=True)
        print(
            "Downloading SoundFont (GeneralUser-GS, ~31MB)... ",
            end="", flush=True, file=sys.stderr,
        )
        urllib.request.urlretrieve(_SOUNDFONT_URL, tmp_path)
        os.rename(tmp_path, dest_path)
        print("done.", file=sys.stderr)
        return dest_path
    except Exception:
        # Clean up partial download
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        print("failed.", file=sys.stderr)
        return None


_cached_soundfont = None
_soundfont_searched = False


def has_fluidsynth():
    """Check if FluidSynth is available."""
    return shutil.which("fluidsynth") is not None


def get_soundfont():
    """Get the path to the SoundFont file, downloading if necessary."""
    global _cached_soundfont, _soundfont_searched
    if _soundfont_searched:
        return _cached_soundfont
    _soundfont_searched = True
    _cached_soundfont = _find_soundfont()
    if _cached_soundfont is None and has_fluidsynth():
        _cached_soundfont = _download_soundfont()
    return _cached_soundfont


def fluidsynth_available():
    """Check if FluidSynth can be used (binary + soundfont)."""
    return has_fluidsynth() and get_soundfont() is not None


# ── MIDI file generation ──────────────────────────────────────────────

def _write_var_len(val):
    """Encode an integer as MIDI variable-length quantity."""
    result = []
    result.append(val & 0x7f)
    val >>= 7
    while val:
        result.append((val & 0x7f) | 0x80)
        val >>= 7
    return bytes(reversed(result))


def _build_midi(midi_notes, duration_seconds, program=0):
    """Build a minimal MIDI file playing the given notes simultaneously."""
    ticks_per_beat = 480
    tempo = 500000  # 120 BPM => 1 beat = 0.5s
    duration_ticks = int(ticks_per_beat * 2 * duration_seconds)

    track_data = b""
    # Tempo meta event
    track_data += b"\x00\xff\x51\x03" + tempo.to_bytes(3, "big")
    # Program change
    track_data += b"\x00\xc0" + bytes([program & 0x7f])

    # Note on for all notes (delta=0 for all)
    for note in midi_notes:
        track_data += b"\x00\x90" + bytes([note & 0x7f, 100])

    # Note off after duration (first note gets the delta, rest get 0)
    for i, note in enumerate(midi_notes):
        delta = _write_var_len(duration_ticks) if i == 0 else b"\x00"
        track_data += delta + b"\x80" + bytes([note & 0x7f, 0])

    # End of track
    track_data += b"\x00\xff\x2f\x00"

    header = b"MThd" + struct.pack(">I", 6) + struct.pack(">HHH", 0, 1, ticks_per_beat)
    track = b"MTrk" + struct.pack(">I", len(track_data)) + track_data

    fd, path = tempfile.mkstemp(suffix=".mid")
    with os.fdopen(fd, "wb") as f:
        f.write(header + track)
    return path


def _render_midi_to_wav(midi_path, soundfont_path):
    """Use FluidSynth to render a MIDI file to WAV."""
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)

    result = subprocess.run(
        [
            "fluidsynth", "-ni",
            "-F", wav_path,
            "-r", str(SAMPLE_RATE),
            "-g", "1.0",
            soundfont_path, midi_path,
        ],
        capture_output=True,
        text=True,
    )

    os.unlink(midi_path)

    if result.returncode != 0 or os.path.getsize(wav_path) == 0:
        try:
            os.unlink(wav_path)
        except OSError:
            pass
        return None

    return wav_path


# ── Core helpers ──────────────────────────────────────────────────────

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


# ── Additive synthesis (fallback) ─────────────────────────────────────

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


def _generate_wav_fallback(midi_note):
    """Generate a WAV file using additive synthesis (no FluidSynth)."""
    freq = midi_to_freq(midi_note)
    duration = _duration_for_midi(midi_note)
    samples = _synthesize_note(freq, duration, AMPLITUDE)

    raw = b"".join(
        struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
        for s in samples
    )
    return _build_wav(raw)


def _generate_chord_wav_fallback(midi_notes):
    """Generate a chord WAV using additive synthesis (no FluidSynth)."""
    if not midi_notes:
        return _generate_wav_fallback(60)

    duration = max(_duration_for_midi(n) for n in midi_notes)
    num_samples = int(SAMPLE_RATE * duration)

    note_samples_list = []
    for midi_note in midi_notes:
        freq = midi_to_freq(midi_note)
        ns = _synthesize_note(freq, duration, AMPLITUDE / len(midi_notes))
        if len(ns) < num_samples:
            ns.extend([0.0] * (num_samples - len(ns)))
        note_samples_list.append(ns)

    raw = b"".join(
        struct.pack("<h", int(max(-1.0, min(1.0, sum(ns[i] for ns in note_samples_list))) * 32767))
        for i in range(num_samples)
    )
    return _build_wav(raw)


def _generate_progression_wav_fallback(chord_sequence, chord_duration=1.0):
    """Generate a progression WAV using additive synthesis (no FluidSynth)."""
    all_raw = b""
    num_samples_per_chord = int(SAMPLE_RATE * chord_duration)

    for midi_notes in chord_sequence:
        note_samples_list = []
        for midi_note in midi_notes:
            freq = midi_to_freq(midi_note)
            ns = _synthesize_note(freq, chord_duration, AMPLITUDE / len(midi_notes))
            if len(ns) < num_samples_per_chord:
                ns.extend([0.0] * (num_samples_per_chord - len(ns)))
            note_samples_list.append(ns[:num_samples_per_chord])

        for i in range(num_samples_per_chord):
            val = sum(ns[i] for ns in note_samples_list)
            clamped = max(-1.0, min(1.0, val))
            all_raw += struct.pack("<h", int(clamped * 32767))

    return _build_wav(all_raw)


def _build_progression_midi(chord_sequence, chord_duration, program=0):
    """Build a MIDI file with a sequence of chords played one after another."""
    ticks_per_beat = 480
    tempo = 500000  # 120 BPM => 1 beat = 0.5s
    duration_ticks = int(ticks_per_beat * 2 * chord_duration)

    track_data = b""
    # Tempo meta event
    track_data += b"\x00\xff\x51\x03" + tempo.to_bytes(3, "big")
    # Program change
    track_data += b"\x00\xc0" + bytes([program & 0x7f])

    for chord_idx, midi_notes in enumerate(chord_sequence):
        # Note on for all notes in this chord (delta=0)
        for note in midi_notes:
            track_data += b"\x00\x90" + bytes([note & 0x7f, 100])

        # Note off after duration (first note gets the delta, rest get 0)
        for i, note in enumerate(midi_notes):
            delta = _write_var_len(duration_ticks) if i == 0 else b"\x00"
            track_data += delta + b"\x80" + bytes([note & 0x7f, 0])

    # End of track
    track_data += b"\x00\xff\x2f\x00"

    header = b"MThd" + struct.pack(">I", 6) + struct.pack(">HHH", 0, 1, ticks_per_beat)
    track = b"MTrk" + struct.pack(">I", len(track_data)) + track_data

    fd, path = tempfile.mkstemp(suffix=".mid")
    with os.fdopen(fd, "wb") as f:
        f.write(header + track)
    return path


# ── Public API ────────────────────────────────────────────────────────

def generate_wav(midi_note, instrument=None):
    """Generate a WAV file for a single MIDI note.

    Args:
        midi_note: MIDI note number (21-108).
        instrument: Index into INSTRUMENTS list, or None for fallback synth.
    """
    if instrument is not None and fluidsynth_available():
        program = INSTRUMENTS[instrument][0]
        sf2 = get_soundfont()
        duration = _duration_for_midi(midi_note)
        midi_path = _build_midi([midi_note], duration, program)
        wav_path = _render_midi_to_wav(midi_path, sf2)
        if wav_path:
            return wav_path
    # Fallback to additive synthesis
    return _generate_wav_fallback(midi_note)


def generate_chord_wav(midi_notes, instrument=None):
    """Generate a WAV file for a chord.

    Args:
        midi_notes: List of MIDI note numbers.
        instrument: Index into INSTRUMENTS list, or None for fallback synth.
    """
    if not midi_notes:
        return generate_wav(60, instrument)

    if instrument is not None and fluidsynth_available():
        program = INSTRUMENTS[instrument][0]
        sf2 = get_soundfont()
        duration = max(_duration_for_midi(n) for n in midi_notes)
        midi_path = _build_midi(midi_notes, duration, program)
        wav_path = _render_midi_to_wav(midi_path, sf2)
        if wav_path:
            return wav_path
    # Fallback
    return _generate_chord_wav_fallback(midi_notes)


def generate_progression_wav(chord_sequence, instrument=None, chord_duration=1.0):
    """Generate a WAV file for a chord progression.

    Args:
        chord_sequence: List of lists of MIDI note numbers (one list per chord).
        instrument: Index into INSTRUMENTS list, or None for fallback synth.
        chord_duration: Duration of each chord in seconds.
    """
    if not chord_sequence:
        return generate_wav(60, instrument)

    if instrument is not None and fluidsynth_available():
        program = INSTRUMENTS[instrument][0]
        sf2 = get_soundfont()
        midi_path = _build_progression_midi(chord_sequence, chord_duration, program)
        wav_path = _render_midi_to_wav(midi_path, sf2)
        if wav_path:
            return wav_path
    # Fallback
    return _generate_progression_wav_fallback(chord_sequence, chord_duration)


def generate_arpeggio_wav(midi_notes, instrument=None, note_duration=0.4):
    """Generate a WAV file that plays chord notes one at a time (arpeggio).

    Args:
        midi_notes: List of MIDI note numbers.
        instrument: Index into INSTRUMENTS list, or None for fallback synth.
        note_duration: Duration of each note in seconds.
    """
    if not midi_notes:
        return generate_wav(60, instrument)
    chord_sequence = [[n] for n in midi_notes]
    return generate_progression_wav(chord_sequence, instrument, note_duration)


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
