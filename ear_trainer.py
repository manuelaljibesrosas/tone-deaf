#!/usr/bin/env python3
"""Ear training program: identifies random notes by ear."""

import curses
import math
import os
import random
import struct
import subprocess
import tempfile
import threading

# Piano range: A0 (MIDI 21) to C8 (MIDI 108)
MIDI_LOW = 21
MIDI_HIGH = 108

SAMPLE_RATE = 44100
DURATION = 2.0
AMPLITUDE = 0.6

# All 12 pitch classes mapped to their semitone offset from C
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

ACCIDENTAL_CYCLE = ["", "#", "b"]  # natural, sharp, flat


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


def generate_wav(midi_note):
    """Generate a WAV file for a given MIDI note with piano-like timbre."""
    freq = midi_to_freq(midi_note)
    num_samples = int(SAMPLE_RATE * DURATION)

    samples = []
    for i in range(num_samples):
        t = i / SAMPLE_RATE

        # Envelope: fast attack, gradual decay
        attack = 0.01
        decay_rate = 1.5
        if t < attack:
            envelope = t / attack
        else:
            envelope = math.exp(-decay_rate * (t - attack))

        # Richer timbre: fundamental + harmonics with decreasing amplitude
        value = 0.0
        harmonics = [(1, 1.0), (2, 0.5), (3, 0.25), (4, 0.12), (5, 0.06)]
        for harmonic, amp in harmonics:
            # Higher harmonics decay faster
            h_envelope = envelope * math.exp(-0.5 * harmonic * t)
            value += amp * h_envelope * math.sin(2 * math.pi * freq * harmonic * t)

        # Normalize by sum of harmonic amplitudes
        value = value / sum(a for _, a in harmonics)
        value *= AMPLITUDE

        clamped = max(-1.0, min(1.0, value))
        samples.append(struct.pack("<h", int(clamped * 32767)))

    raw = b"".join(samples)
    wav_header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(raw),
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        1,  # mono
        SAMPLE_RATE,
        SAMPLE_RATE * 2,
        2,  # block align
        16,  # bits per sample
        b"data",
        len(raw),
    )

    fd, path = tempfile.mkstemp(suffix=".wav")
    with os.fdopen(fd, "wb") as f:
        f.write(wav_header + raw)
    return path


def play_wav_async(path):
    """Play a WAV file asynchronously, return the process."""
    proc = subprocess.Popen(
        ["paplay", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Clean up temp file after playback finishes
    def cleanup():
        proc.wait()
        try:
            os.unlink(path)
        except OSError:
            pass
    threading.Thread(target=cleanup, daemon=True).start()
    return proc


def draw_box(stdscr, y, x, h, w):
    """Draw a box with Unicode box-drawing characters."""
    # Corners
    stdscr.addstr(y, x, "┌")
    stdscr.addstr(y, x + w - 1, "┐")
    stdscr.addstr(y + h - 1, x, "└")
    stdscr.addstr(y + h - 1, x + w - 1, "┘")
    # Horizontal lines
    for col in range(x + 1, x + w - 1):
        stdscr.addstr(y, col, "─")
        stdscr.addstr(y + h - 1, col, "─")
    # Vertical lines
    for row in range(y + 1, y + h - 1):
        stdscr.addstr(row, x, "│")
        stdscr.addstr(row, x + w - 1, "│")


def draw_separator(stdscr, y, x, w):
    """Draw a horizontal separator with T-junctions."""
    stdscr.addstr(y, x, "├")
    stdscr.addstr(y, x + w - 1, "┤")
    for col in range(x + 1, x + w - 1):
        stdscr.addstr(y, col, "─")


def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.keypad(True)

    correct = 0
    total = 0
    current_note = None
    current_wav = None
    answer_letter = ""
    accidental_idx = 0  # index into ACCIDENTAL_CYCLE
    feedback = ""
    feedback_attr = curses.A_NORMAL

    BOX_W = 47
    BOX_H = 10

    def pick_note():
        nonlocal current_note, current_wav
        current_note = random.randint(MIDI_LOW, MIDI_HIGH)
        current_wav = generate_wav(current_note)

    def play_current():
        if current_wav and os.path.exists(current_wav):
            # Re-generate since cleanup might have deleted it
            path = generate_wav(current_note)
        else:
            path = current_wav or generate_wav(current_note)
        play_wav_async(path)

    def get_answer_str():
        if not answer_letter:
            return "_"
        return answer_letter + ACCIDENTAL_CYCLE[accidental_idx]

    def check_answer():
        nonlocal correct, total, feedback, feedback_attr
        answer = answer_letter + ACCIDENTAL_CYCLE[accidental_idx]
        if answer not in NAME_TO_PITCH_CLASS:
            feedback = f"  Invalid note: {answer}"
            feedback_attr = curses.A_BOLD
            return False
        answer_pc = NAME_TO_PITCH_CLASS[answer]
        actual_pc = midi_to_pitch_class(current_note)
        total += 1
        note_name = midi_to_name(current_note)
        if answer_pc == actual_pc:
            correct += 1
            feedback = f"  \u2713 Correct! It was {note_name}"
            feedback_attr = curses.A_BOLD
        else:
            feedback = f"  \u2717 Wrong! It was {note_name} (you said: {answer})"
            feedback_attr = curses.A_BOLD
        return True

    def render():
        stdscr.erase()
        y0, x0 = 1, 2

        # Score string
        if total == 0:
            score_str = "Score: 0/0 -%"
        else:
            pct = int(100 * correct / total)
            score_str = f"Score: {correct}/{total} {pct}%"

        draw_box(stdscr, y0, x0, BOX_H, BOX_W)
        draw_separator(stdscr, y0 + 2, x0, BOX_W)

        # Header
        header = "  EAR TRAINER"
        stdscr.addstr(y0 + 1, x0 + 1, header, curses.A_BOLD)
        stdscr.addstr(y0 + 1, x0 + BOX_W - 2 - len(score_str), score_str)

        # Feedback line
        row = y0 + 3
        if feedback:
            stdscr.addstr(row, x0 + 1, feedback[:BOX_W - 3], feedback_attr)
            row += 1

        # Listen prompt
        stdscr.addstr(row, x0 + 1, "  \u266a Listen...", curses.A_NORMAL)
        row += 1

        # Answer
        row += 1
        answer_display = get_answer_str()
        stdscr.addstr(row, x0 + 1, f"  Your answer:  {answer_display}", curses.A_NORMAL)

        # Controls
        stdscr.addstr(y0 + BOX_H - 3, x0 + 1, "  [A-G] note  [Tab] #/b  [Enter] submit")
        stdscr.addstr(y0 + BOX_H - 2, x0 + 1, "  [R] replay  [Esc] quit")

        stdscr.refresh()

    # Start first round
    pick_note()
    play_current()
    render()

    while True:
        key = stdscr.getch()

        if key == 27:  # Esc
            break
        elif key == ord("\t"):  # Tab
            if answer_letter:
                accidental_idx = (accidental_idx + 1) % 3
        elif key in (ord("\n"), ord("\r")):  # Enter
            if answer_letter:
                if check_answer():
                    render()
                    stdscr.refresh()
                    # Brief pause to show feedback, then next note
                    curses.napms(1200)
                    answer_letter = ""
                    accidental_idx = 0
                    pick_note()
                    play_current()
        elif key == ord("r") or key == ord("R"):
            play_current()
        elif ord("a") <= key <= ord("g") or ord("A") <= key <= ord("G"):
            answer_letter = chr(key).upper()
            accidental_idx = 0  # reset accidental when picking new letter

        render()


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass

    # Print final score after curses exits
    print("\nThanks for practicing!")
