#!/usr/bin/env python3
"""
SATB Bach chorale generator using Coconet (Google Magenta).

Usage:
    ./venv12/bin/python3 generate.py

Controls:
    Enter   – generate and play next chorale
    q       – quit
"""

import argparse
import random
import os
import sys
import subprocess
import tempfile
import threading
import warnings

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
warnings.filterwarnings('ignore')

import numpy as np

import tensorflow.compat.v1 as tf
tf.disable_v2_behavior()
tf.logging.set_verbosity(tf.logging.ERROR)

from magenta.models.coconet import lib_tfsampling, lib_hparams, lib_pianoroll

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CHECKPOINT   = os.path.join(SCRIPT_DIR, 'weights', 'coconet_checkpoint',
                             'coconet-64layers-128filters')
SOUNDFONT    = os.path.abspath(os.path.join(SCRIPT_DIR, '..', 'soundfonts',
                                             'GeneralUser-GS.sf2'))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VOICE_NAMES     = ['S', 'A', 'T', 'B']
NOTE_NAMES      = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
STEPS_PER_BEAT  = 4   # 16th-note resolution
BEATS_PER_BAR   = 4
BARS            = 4
PIECE_LEN       = BARS * BEATS_PER_BAR * STEPS_PER_BEAT  # 64
TEMPERATURE     = 0.99
# Gibbs steps: default 0 = tt*instruments = 256, too slow on CPU.
# 64 = one full pass per timestep; good quality/speed tradeoff (~10s).
GIBBS_STEPS     = 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def midi_to_name(pitch: int) -> str:
    octave = pitch // 12 - 1
    return f"{NOTE_NAMES[pitch % 12]}{octave}"


def pianoroll_to_grid(pianoroll, min_pitch: int):
    """
    pianoroll: (time, num_pitches, num_instruments)
    Returns: list of bars, each a list of beats, each a list of 4 note-name strings.
    """
    tt, pp, ii = pianoroll.shape
    grid = []
    for bar in range(BARS):
        bar_beats = []
        for beat in range(BEATS_PER_BAR):
            t = bar * BEATS_PER_BAR * STEPS_PER_BEAT + beat * STEPS_PER_BEAT
            beat_notes = []
            for instr in range(ii):
                active = np.where(pianoroll[t, :, instr] > 0.5)[0]
                if len(active):
                    beat_notes.append(midi_to_name(active[0] + min_pitch))
                else:
                    # Look a step ahead in case note starts mid-beat
                    found = False
                    for dt in range(1, STEPS_PER_BEAT):
                        if t + dt < tt:
                            active2 = np.where(pianoroll[t + dt, :, instr] > 0.5)[0]
                            if len(active2):
                                beat_notes.append(midi_to_name(active2[0] + min_pitch))
                                found = True
                                break
                    if not found:
                        beat_notes.append('—')
            bar_beats.append(beat_notes)
        grid.append(bar_beats)
    return grid


def print_melody(grid):
    """Single-row display: one note per beat, all bars on one line."""
    col_w = 6
    SOPRANO = 0
    line = ''
    for b, bar in enumerate(grid):
        line += '| '
        for beat_notes in bar:
            line += f"{beat_notes[SOPRANO]:<{col_w}}"
    line += '|'

    bar_labels = '  '.join(f'Bar {b+1}' + ' ' * (col_w * BEATS_PER_BAR - 3)
                            for b in range(BARS))
    print(f"  {bar_labels}")
    print(f"  {line}")
    print()


def print_chorale(grid):
    # Header
    bar_header = ''.join(f'  Bar {b+1}' + ' ' * (col_w * BEATS_PER_BAR - 6)
                          for b in range(BARS))
    print(f"     {bar_header}")

    sep = '     ' + ('+' + '-' * col_w) * (BARS * BEATS_PER_BAR) + '+'
    print(sep)

    for vi, vname in enumerate(VOICE_NAMES):
        cells = ''
        for bar in grid:
            for beat_notes in bar:
                note = beat_notes[vi]
                cells += f'| {note:<{col_w - 2}} '
        print(f'  {vname}  {cells}|')

    print(sep)


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
_playback_proc = None
_playback_lock = threading.Lock()


def stop_playback():
    global _playback_proc
    with _playback_lock:
        if _playback_proc and _playback_proc.poll() is None:
            _playback_proc.terminate()
            _playback_proc.wait()
        _playback_proc = None


def play_midi(midi_data):
    """Render with fluidsynth, play with paplay (non-blocking thread)."""
    global _playback_proc

    tmpdir = tempfile.mkdtemp()
    midi_path = os.path.join(tmpdir, 'chorale.mid')
    wav_path  = os.path.join(tmpdir, 'chorale.wav')
    midi_data.write(midi_path)

    # Render
    render = subprocess.run(
        ['fluidsynth', '-ni', '-F', wav_path, '-r', '44100', SOUNDFONT, midi_path],
        capture_output=True
    )
    if render.returncode != 0:
        print(f"\nfluidsynth failed: {render.stderr.decode()[:200]}", file=sys.stderr)
        return

    stop_playback()

    with _playback_lock:
        _playback_proc = subprocess.Popen(['paplay', wav_path])


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
def load_model():
    print("Loading Coconet model ... ", end='', flush=True)
    hparams = lib_hparams.load_hparams(CHECKPOINT)
    sampler  = lib_tfsampling.CoconetSampleGraph(CHECKPOINT)
    decoder  = lib_pianoroll.get_pianoroll_encoder_decoder(hparams)
    # Trigger graph build + checkpoint restore with a dummy run
    dummy = np.zeros([1, PIECE_LEN, hparams.num_pitches, hparams.num_instruments],
                     dtype=np.float32)
    sampler.run(dummy, sample_steps=1, total_gibbs_steps=1, temperature=TEMPERATURE)
    print("done.")
    return sampler, hparams, decoder


def generate(sampler, hparams, melody=False):
    """Generate one 4-bar SATB pianoroll. If melody=True, returns soprano only."""
    pianorolls = np.zeros(
        [1, PIECE_LEN, hparams.num_pitches, hparams.num_instruments],
        dtype=np.float32)
    result    = sampler.run(pianorolls, temperature=TEMPERATURE,
                           total_gibbs_steps=GIBBS_STEPS)
    pianoroll = result['pianorolls'][0]   # (T, P, I)

    decoder  = lib_pianoroll.get_pianoroll_encoder_decoder(hparams)

    if melody:
        # Keep only instrument 0 (soprano), silence the rest
        mono = pianoroll.copy()
        mono[:, :, 1:] = 0
        midi_out = decoder.decode_to_midi(mono)
        return pianoroll, midi_out   # return full roll for grid, mono midi for audio
    else:
        midi_out = decoder.decode_to_midi(pianoroll)
        return pianoroll, midi_out


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Bach chorale / melody generator')
    parser.add_argument('--melody', action='store_true',
                        help='Generate soprano melody only (default: full SATB chorale)')
    args = parser.parse_args()
    melody_mode = args.melody

    sampler, hparams, decoder = load_model()

    mode_label = 'Melody' if melody_mode else 'Chorale'
    print()
    print(f"Bach {mode_label} Generator — Coconet / Magenta")
    print("Press Enter to generate, 'q' + Enter to quit.")
    print()

    count = 0
    while True:
        try:
            user = input(f"[ Enter = generate {mode_label.lower()} | q = quit ] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if user == 'q':
            break

        count += 1
        print(f"\nGenerating {mode_label.lower()} #{count} ...", end='', flush=True)
        pianoroll, midi_out = generate(sampler, hparams, melody=melody_mode)
        print(" done.\n")

        grid = pianoroll_to_grid(pianoroll, hparams.min_pitch)
        if melody_mode:
            print_melody(grid)
        else:
            print_chorale(grid)
            print()

        # Start playback in background thread so user can queue next
        threading.Thread(target=play_midi, args=(midi_out,), daemon=True).start()

    stop_playback()
    print("\nBye.")


if __name__ == '__main__':
    main()
