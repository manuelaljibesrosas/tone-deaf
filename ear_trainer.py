#!/usr/bin/env python3
"""Ear training program: note identification and chord recognition."""

import curses
import random
from datetime import datetime

from config import (
    load_chord_training_config,
    load_key_training_config,
    load_note_training_config,
    save_chord_training_config,
    save_key_training_config,
    save_note_training_config,
    save_session,
)
from synth import (
    ACCIDENTAL_CYCLE,
    INSTRUMENTS,
    INSTRUMENT_NAMES,
    MIDI_HIGH,
    MIDI_LOW,
    NAME_TO_PITCH_CLASS,
    NOTE_NAMES,
    fluidsynth_available,
    generate_chord_wav,
    generate_arpeggio_wav,
    generate_progression_wav,
    generate_wav,
    instrument_range,
    midi_to_name,
    midi_to_pitch_class,
    play_wav_async,
)

# ── Chord definitions ──────────────────────────────────────────────────

# Intervals in semitones from root for each chord quality
CHORD_INTERVALS = {
    "Maj":  [0, 4, 7],
    "Min":  [0, 3, 7],
    "Aug":  [0, 4, 8],
    "Dim":  [0, 3, 6],
    "Dom7": [0, 4, 7, 10],
    "Maj7": [0, 4, 7, 11],
    "Min7": [0, 3, 7, 10],
}

TRIAD_TYPES = ["Maj", "Min", "Aug", "Dim"]
SEVENTH_TYPES = ["Dom7", "Maj7", "Min7"]
ALL_CHORD_TYPES = TRIAD_TYPES + SEVENTH_TYPES

INVERSION_NAMES = ["root", "1st", "2nd", "3rd"]


def build_chord_midi(root_midi, chord_type, inversion):
    """Build list of MIDI notes for a chord with given root, type, and inversion."""
    intervals = list(CHORD_INTERVALS[chord_type])
    num_notes = len(intervals)

    if inversion > 0 and inversion < num_notes:
        # Rotate intervals: move bottom notes up an octave
        for _ in range(inversion):
            intervals.append(intervals.pop(0) + 12)

    return [root_midi + iv for iv in intervals]


def max_inversion_for(chord_type):
    """Return the maximum inversion index for a chord type."""
    return len(CHORD_INTERVALS[chord_type]) - 1


# ── UI drawing helpers ─────────────────────────────────────────────────

def draw_box(stdscr, y, x, h, w):
    try:
        stdscr.addstr(y, x, "┌")
        stdscr.addstr(y, x + w - 1, "┐")
        stdscr.addstr(y + h - 1, x, "└")
        # Bottom-right corner: use insstr to avoid curses wrapping error
        try:
            stdscr.addstr(y + h - 1, x + w - 1, "┘")
        except curses.error:
            pass
        for col in range(x + 1, x + w - 1):
            stdscr.addstr(y, col, "─")
            try:
                stdscr.addstr(y + h - 1, col, "─")
            except curses.error:
                pass
        for row in range(y + 1, y + h - 1):
            stdscr.addstr(row, x, "│")
            try:
                stdscr.addstr(row, x + w - 1, "│")
            except curses.error:
                pass
    except curses.error:
        pass


def draw_separator(stdscr, y, x, w):
    try:
        stdscr.addstr(y, x, "├")
        stdscr.addstr(y, x + w - 1, "┤")
        for col in range(x + 1, x + w - 1):
            stdscr.addstr(y, col, "─")
    except curses.error:
        pass


def safe_addstr(stdscr, y, x, text, attr=curses.A_NORMAL):
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def is_up(key):
    return key == curses.KEY_UP or key == ord("k")

def is_down(key):
    return key == curses.KEY_DOWN or key == ord("j")

def is_left(key):
    return key == curses.KEY_LEFT or key == ord("h")

def is_right(key):
    return key == curses.KEY_RIGHT or key == ord("l")


# ── Mode selection screen ──────────────────────────────────────────────

def mode_select_screen(stdscr):
    """Show mode selection. Returns 'note', 'chord', 'key', or None (quit)."""
    BOX_W = 47
    BOX_H = 11
    selected = 0
    modes = [("Note Training", "note"), ("Chord Training", "chord"), ("Key Training", "key")]

    while True:
        stdscr.erase()
        y0, x0 = 1, 2
        draw_box(stdscr, y0, x0, BOX_H, BOX_W)
        draw_separator(stdscr, y0 + 2, x0, BOX_W)
        safe_addstr(stdscr, y0 + 1, x0 + 2, "EAR TRAINER", curses.A_BOLD)

        for i, (label, _) in enumerate(modes):
            marker = " > " if i == selected else "   "
            attr = curses.A_BOLD if i == selected else curses.A_NORMAL
            safe_addstr(stdscr, y0 + 4 + i, x0 + 2, f"{marker}{label}", attr)

        safe_addstr(stdscr, y0 + BOX_H - 3, x0 + 2, "[j/\u2193 k/\u2191] select  [Enter] start")
        safe_addstr(stdscr, y0 + BOX_H - 2, x0 + 2, "[Esc] quit")
        stdscr.refresh()

        key = stdscr.getch()
        if key == 27:
            return None
        elif is_up(key):
            selected = (selected - 1) % len(modes)
        elif is_down(key):
            selected = (selected + 1) % len(modes)
        elif key in (ord("\n"), ord("\r")):
            return modes[selected][1]


# ── Note selection view ────────────────────────────────────────────────

# Display labels for the 12 pitch classes
PITCH_CLASS_LABELS = [
    "C", "C#/Db", "D", "D#/Eb", "E", "F",
    "F#/Gb", "G", "G#/Ab", "A", "A#/Bb", "B",
]

# Map letter keys to list of pitch classes they can toggle
LETTER_TO_PC = {
    "C": [0, 1],   # C, C#/Db
    "D": [2, 3],   # D, D#/Eb
    "E": [4],       # E
    "F": [5, 6],   # F, F#/Gb
    "G": [7, 8],   # G, G#/Ab
    "A": [9, 10],  # A, A#/Bb
    "B": [11],      # B
}


def note_selection_screen(stdscr, enabled_pcs):
    """Show note selection. enabled_pcs is a set of pitch classes (0-11).
    Returns updated set, or original set if cancelled."""
    BOX_W = 50
    BOX_H = 13
    selected = set(enabled_pcs)  # work on a copy
    cursor = 0  # index into PITCH_CLASS_LABELS (0-11)

    while True:
        stdscr.erase()
        y0, x0 = 1, 2
        draw_box(stdscr, y0, x0, BOX_H, BOX_W)
        draw_separator(stdscr, y0 + 2, x0, BOX_W)
        safe_addstr(stdscr, y0 + 1, x0 + 2, "NOTE SELECTION", curses.A_BOLD)

        # Draw notes in 3 rows of 4
        for i, label in enumerate(PITCH_CLASS_LABELS):
            row_offset = i // 4
            col_offset = i % 4
            r = y0 + 4 + row_offset
            c = x0 + 3 + col_offset * 11

            check = "x" if i in selected else " "
            marker = ">" if i == cursor else " "
            attr = curses.A_BOLD if i == cursor else curses.A_NORMAL
            safe_addstr(stdscr, r, c, f"{marker}[{check}] {label:<5}", attr)

        safe_addstr(stdscr, y0 + BOX_H - 4, x0 + 2, "[h/\u2190 j/\u2193 k/\u2191 l/\u2192] move  [Space] toggle")
        safe_addstr(stdscr, y0 + BOX_H - 3, x0 + 2, "[A] all on  [X] all off")
        safe_addstr(stdscr, y0 + BOX_H - 2, x0 + 2, "[Enter] confirm  [Esc] cancel")
        stdscr.refresh()

        key = stdscr.getch()
        if key == 27:
            return enabled_pcs
        elif is_right(key):
            cursor = (cursor + 1) % 12
        elif is_left(key):
            cursor = (cursor - 1) % 12
        elif is_down(key):
            cursor = min(cursor + 4, 11)
        elif is_up(key):
            cursor = max(cursor - 4, 0)
        elif key == ord(" "):
            if cursor in selected:
                if len(selected) > 1:
                    selected.discard(cursor)
            else:
                selected.add(cursor)
        elif key in (ord("a"), ord("A")):
            selected = set(range(12))
        elif key in (ord("x"), ord("X")):
            # Keep at least one
            selected = {cursor}
        elif key in (ord("\n"), ord("\r")):
            if len(selected) >= 1:
                return selected


# ── Instrument selection view ──────────────────────────────────────────

def instrument_selection_screen(stdscr, current_instrument):
    """Select an instrument. Returns index into INSTRUMENTS or None for synth.
    current_instrument is the current index or None."""
    BOX_W = 50
    BOX_H = 15

    has_fs = fluidsynth_available()

    # Options: "Synth (built-in)" + all instruments
    options = ["Synth (built-in)"] + [name for _, name, _, _ in INSTRUMENTS]
    cursor = 0 if current_instrument is None else current_instrument + 1

    while True:
        stdscr.erase()
        y0, x0 = 1, 2
        draw_box(stdscr, y0, x0, BOX_H, BOX_W)
        draw_separator(stdscr, y0 + 2, x0, BOX_W)
        safe_addstr(stdscr, y0 + 1, x0 + 2, "INSTRUMENT", curses.A_BOLD)

        if not has_fs:
            safe_addstr(stdscr, y0 + 1, x0 + 15,
                        "(no SoundFont found)", curses.A_NORMAL)

        for i, label in enumerate(options):
            r = y0 + 3 + i
            if r >= y0 + BOX_H - 3:
                break
            marker = " > " if i == cursor else "   "
            attr = curses.A_BOLD if i == cursor else curses.A_NORMAL
            # Dim instruments if fluidsynth not available
            if i > 0 and not has_fs:
                attr = curses.A_DIM
            safe_addstr(stdscr, r, x0 + 2, f"{marker}{label}", attr)

        safe_addstr(stdscr, y0 + BOX_H - 3, x0 + 2,
                    "[j/\u2193 k/\u2191] select  [Enter] confirm")
        safe_addstr(stdscr, y0 + BOX_H - 2, x0 + 2, "[Esc] cancel")
        stdscr.refresh()

        key = stdscr.getch()
        if key == 27:
            return current_instrument
        elif is_up(key):
            cursor = (cursor - 1) % len(options)
        elif is_down(key):
            cursor = (cursor + 1) % len(options)
        elif key in (ord("\n"), ord("\r")):
            if cursor == 0:
                return None  # built-in synth
            if not has_fs:
                continue  # can't select instruments without fluidsynth
            return cursor - 1  # index into INSTRUMENTS


# ── Chord selection view ───────────────────────────────────────────────

def chord_selection_screen(stdscr, enabled_chords, enabled_inversions):
    """Configure which chord types and inversions to test.
    Returns (enabled_chords, enabled_inversions) sets, or originals if cancelled."""
    BOX_W = 50
    BOX_H = 16

    chords = set(enabled_chords)
    inversions = set(enabled_inversions)

    # Build flat list of toggleable items for cursor navigation
    # Items: (category, index, label)
    items = []
    for ct in TRIAD_TYPES:
        items.append(("chord", ct, ct))
    for ct in SEVENTH_TYPES:
        items.append(("chord", ct, ct))
    for i, name in enumerate(INVERSION_NAMES):
        items.append(("inv", i, name))

    cursor = 0

    while True:
        stdscr.erase()
        y0, x0 = 1, 2
        draw_box(stdscr, y0, x0, BOX_H, BOX_W)
        draw_separator(stdscr, y0 + 2, x0, BOX_W)
        safe_addstr(stdscr, y0 + 1, x0 + 2, "CHORD SELECTION", curses.A_BOLD)

        # Triads row
        safe_addstr(stdscr, y0 + 3, x0 + 3, "Triads:", curses.A_NORMAL)
        for i, ct in enumerate(TRIAD_TYPES):
            idx = i  # index in items
            check = "x" if ct in chords else " "
            marker = ">" if cursor == idx else " "
            attr = curses.A_BOLD if cursor == idx else curses.A_NORMAL
            safe_addstr(stdscr, y0 + 4, x0 + 3 + i * 11, f"{marker}[{check}] {ct:<4}", attr)

        # Sevenths row
        safe_addstr(stdscr, y0 + 6, x0 + 3, "Sevenths:", curses.A_NORMAL)
        for i, ct in enumerate(SEVENTH_TYPES):
            idx = len(TRIAD_TYPES) + i
            check = "x" if ct in chords else " "
            marker = ">" if cursor == idx else " "
            attr = curses.A_BOLD if cursor == idx else curses.A_NORMAL
            safe_addstr(stdscr, y0 + 7, x0 + 3 + i * 11, f"{marker}[{check}] {ct:<4}", attr)

        # Inversions row
        safe_addstr(stdscr, y0 + 9, x0 + 3, "Inversions:", curses.A_NORMAL)
        for i, name in enumerate(INVERSION_NAMES):
            idx = len(ALL_CHORD_TYPES) + i
            check = "x" if i in inversions else " "
            marker = ">" if cursor == idx else " "
            attr = curses.A_BOLD if cursor == idx else curses.A_NORMAL
            safe_addstr(stdscr, y0 + 10, x0 + 3 + i * 11, f"{marker}[{check}] {name:<4}", attr)

        safe_addstr(stdscr, y0 + BOX_H - 3, x0 + 2, "[h/\u2190 j/\u2193 k/\u2191 l/\u2192] move  [Space] toggle")
        safe_addstr(stdscr, y0 + BOX_H - 2, x0 + 2, "[Enter] confirm  [Esc] cancel")
        stdscr.refresh()

        key = stdscr.getch()
        if key == 27:
            return enabled_chords, enabled_inversions
        elif is_right(key):
            cursor = (cursor + 1) % len(items)
        elif is_left(key):
            cursor = (cursor - 1) % len(items)
        elif is_down(key) or is_up(key):
            # Move between sections: triads(0-3), sevenths(4-6), inversions(7-10)
            triad_end = len(TRIAD_TYPES)
            seventh_end = triad_end + len(SEVENTH_TYPES)
            if is_down(key):
                if cursor < triad_end:
                    cursor = min(cursor + triad_end, seventh_end - 1)
                elif cursor < seventh_end:
                    col = cursor - triad_end
                    cursor = min(seventh_end + col, len(items) - 1)
            else:  # UP
                if cursor >= seventh_end:
                    col = cursor - seventh_end
                    cursor = triad_end + min(col, len(SEVENTH_TYPES) - 1)
                elif cursor >= triad_end:
                    col = cursor - triad_end
                    cursor = min(col, triad_end - 1)
        elif key == ord(" "):
            cat, val, _ = items[cursor]
            if cat == "chord":
                if val in chords:
                    if len(chords) > 1:
                        chords.discard(val)
                else:
                    chords.add(val)
            else:  # inv
                if val in inversions:
                    if len(inversions) > 1:
                        inversions.discard(val)
                else:
                    inversions.add(val)
        elif key in (ord("\n"), ord("\r")):
            return chords, inversions


# ── Note training mode ─────────────────────────────────────────────────

def note_training(stdscr):
    """Note ear training loop. Plays a sequence of notes; the user identifies
    each one in order, then sees per-note feedback after the full sequence."""
    saved_pcs, saved_instr, octave_mode, sequence_length = load_note_training_config()
    enabled_pcs = saved_pcs
    instrument = saved_instr
    correct = 0
    total = 0
    started_at = datetime.now().isoformat()

    MAX_SEQ = 8
    sequence_length = max(1, min(MAX_SEQ, sequence_length))

    sequence_notes = []      # MIDI notes for the current challenge
    answers = []             # submitted answers: list of (letter, accidental_idx, octave)
    results = []             # per-note evaluation dicts (feedback phase)
    answer_idx = 0           # which note is being answered
    phase = "answering"      # "answering" or "feedback"

    answer_letter = ""
    accidental_idx = 0
    answer_octave = None     # int 0-8 when octave_mode is on
    feedback = ""            # transient message (e.g. invalid input)
    feedback_attr = curses.A_NORMAL

    BOX_W = 50

    def pick_sequence():
        nonlocal sequence_notes, answers, results, answer_idx, phase
        nonlocal answer_letter, accidental_idx, answer_octave, feedback
        lo, hi = instrument_range(instrument)
        notes = []
        for _ in range(sequence_length):
            while True:
                n = random.randint(lo, hi)
                if midi_to_pitch_class(n) in enabled_pcs:
                    notes.append(n)
                    break
        sequence_notes = notes
        answers = []
        results = []
        answer_idx = 0
        phase = "answering"
        answer_letter = ""
        accidental_idx = 0
        answer_octave = None
        feedback = ""

    def play_sequence():
        path = generate_arpeggio_wav(sequence_notes, instrument, note_duration=0.6)
        play_wav_async(path)

    def get_answer_str():
        if not answer_letter:
            return "_"
        s = answer_letter + ACCIDENTAL_CYCLE[accidental_idx]
        if octave_mode:
            s += str(answer_octave) if answer_octave is not None else "_"
        return s

    def submit_note():
        """Validate and record the current note answer. Returns True if accepted."""
        nonlocal answer_idx, answer_letter, accidental_idx, answer_octave
        nonlocal feedback, feedback_attr
        answer = answer_letter + ACCIDENTAL_CYCLE[accidental_idx]
        if answer not in NAME_TO_PITCH_CLASS:
            feedback = "  Invalid note: " + answer
            feedback_attr = curses.A_BOLD
            return False
        if octave_mode and answer_octave is None:
            feedback = "  Enter octave (0-8)"
            feedback_attr = curses.A_BOLD
            return False
        answers.append((answer_letter, accidental_idx, answer_octave))
        answer_idx += 1
        answer_letter = ""
        accidental_idx = 0
        answer_octave = None
        feedback = ""
        if answer_idx >= sequence_length:
            evaluate()
        return True

    def evaluate():
        """Score the full sequence and build feedback results."""
        nonlocal correct, total, results, phase
        results = []
        for i, note in enumerate(sequence_notes):
            letter, acc_idx, octv = answers[i]
            answer = letter + ACCIDENTAL_CYCLE[acc_idx]
            answer_pc = NAME_TO_PITCH_CLASS[answer]
            actual_pc = midi_to_pitch_class(note)
            actual_octave = (note - 12) // 12
            pc_correct = answer_pc == actual_pc
            oct_correct = (not octave_mode) or (octv == actual_octave)
            is_correct = pc_correct and oct_correct
            total += 1
            if is_correct:
                correct += 1
            if octave_mode:
                actual_str = midi_to_name(note)
                answer_str = answer + (str(octv) if octv is not None else "")
            else:
                actual_str = NOTE_NAMES[actual_pc]
                answer_str = answer
            results.append({
                "correct": is_correct,
                "actual": actual_str,
                "answer": answer_str,
            })
        phase = "feedback"

    def feedback_lines():
        """Wrap per-note result tokens into lines that fit the box interior."""
        tokens = []
        for r in results:
            if r["correct"]:
                tokens.append(f"\u2713{r['actual']}")
            else:
                tokens.append(f"\u2717{r['answer']}\u2192{r['actual']}")
        iw = BOX_W - 6
        lines = []
        cur = ""
        for tok in tokens:
            piece = tok if not cur else cur + "  " + tok
            if len(piece) > iw and cur:
                lines.append(cur)
                cur = tok
            else:
                cur = piece
        if cur:
            lines.append(cur)
        return lines or ["(no answers)"]

    def instrument_label():
        if instrument is None:
            return "Synth"
        return INSTRUMENT_NAMES[instrument]

    def render():
        stdscr.erase()
        y0, x0 = 1, 2

        if total == 0:
            score_str = "Score: 0/0 -%"
        else:
            pct = int(100 * correct / total)
            score_str = f"Score: {correct}/{total} {pct}%"

        # Build body content lines as (text, attr)
        body = []
        oct_label = " [octave ON]" if octave_mode else ""
        body.append((f"\u266a Listen...  ({instrument_label()})   [Len: {sequence_length}]",
                     curses.A_NORMAL))
        body.append(("", curses.A_NORMAL))

        if phase == "answering":
            prog = f"Note {answer_idx + 1}/{sequence_length}:  {get_answer_str()}{oct_label}"
            body.append((prog, curses.A_NORMAL))
            if feedback:
                body.append((feedback, feedback_attr))
        else:  # feedback
            for line in feedback_lines():
                body.append((line, curses.A_BOLD))

        # Controls
        if phase == "answering":
            controls = [
                "[A-G] note  [Tab] #/b  [Enter] next",
                "[R] replay  [N] notes  [+/-] seq length",
                f"[I] instrument  [O] octave mode{'  [0-8] octave' if octave_mode else ''}",
                "[Esc] back to menu",
            ]
        else:
            controls = [
                "[Enter] next sequence  [R] replay",
                "[N] notes  [+/-] seq length",
                "[I] instrument  [O] octave mode",
                "[Esc] back to menu",
            ]

        body_start = y0 + 3
        gap_row = body_start + len(body)
        controls_start = gap_row + 1
        bottom = controls_start + len(controls)
        BOX_H = bottom - y0 + 1

        draw_box(stdscr, y0, x0, BOX_H, BOX_W)
        draw_separator(stdscr, y0 + 2, x0, BOX_W)
        safe_addstr(stdscr, y0 + 1, x0 + 2, "NOTE TRAINER", curses.A_BOLD)
        safe_addstr(stdscr, y0 + 1, x0 + BOX_W - 2 - len(score_str), score_str)

        for i, (text, attr) in enumerate(body):
            if text:
                safe_addstr(stdscr, body_start + i, x0 + 2, text[:BOX_W - 4], attr)

        for i, text in enumerate(controls):
            safe_addstr(stdscr, controls_start + i, x0 + 2, text)

        stdscr.refresh()

    def reset_and_restart():
        nonlocal sequence_length
        sequence_length = max(1, min(MAX_SEQ, sequence_length))
        pick_sequence()
        play_sequence()

    pick_sequence()
    play_sequence()
    render()

    while True:
        key = stdscr.getch()

        if key == 27:
            save_note_training_config(enabled_pcs, instrument, octave_mode, sequence_length)
            if total > 0:
                save_session(
                    "note", started_at, datetime.now().isoformat(),
                    correct, total, instrument,
                    {
                        "enabled_pcs": sorted(enabled_pcs),
                        "octave_mode": octave_mode,
                        "sequence_length": sequence_length,
                    },
                )
            return
        elif key in (ord("n"), ord("N")):
            enabled_pcs = note_selection_screen(stdscr, enabled_pcs)
            save_note_training_config(enabled_pcs, instrument, octave_mode, sequence_length)
            reset_and_restart()
        elif key in (ord("i"), ord("I")):
            instrument = instrument_selection_screen(stdscr, instrument)
            save_note_training_config(enabled_pcs, instrument, octave_mode, sequence_length)
            reset_and_restart()
        elif key in (ord("o"), ord("O")):
            octave_mode = not octave_mode
            save_note_training_config(enabled_pcs, instrument, octave_mode, sequence_length)
            reset_and_restart()
        elif key in (ord("+"), ord("=")):
            if sequence_length < MAX_SEQ:
                sequence_length += 1
                save_note_training_config(enabled_pcs, instrument, octave_mode, sequence_length)
                reset_and_restart()
        elif key in (ord("-"), ord("_")):
            if sequence_length > 1:
                sequence_length -= 1
                save_note_training_config(enabled_pcs, instrument, octave_mode, sequence_length)
                reset_and_restart()
        elif key in (ord("r"), ord("R")):
            play_sequence()
        elif phase == "answering":
            if key == ord("\t"):
                if answer_letter:
                    accidental_idx = (accidental_idx + 1) % 3
            elif key in (ord("\n"), ord("\r")):
                if answer_letter:
                    submit_note()
            elif octave_mode and ord("0") <= key <= ord("8"):
                answer_octave = key - ord("0")
            elif ord("a") <= key <= ord("g") or ord("A") <= key <= ord("G"):
                answer_letter = chr(key).upper()
                accidental_idx = 0
        elif phase == "feedback":
            if key in (ord("\n"), ord("\r")):
                pick_sequence()
                play_sequence()

        render()


# ── Chord training mode ────────────────────────────────────────────────

def chord_training(stdscr):
    """Chord recognition ear training loop."""
    saved_chords, saved_inv, saved_instr = load_chord_training_config()
    enabled_chords = saved_chords if saved_chords is not None else set(ALL_CHORD_TYPES)
    enabled_inversions = saved_inv if saved_inv is not None else {0, 1, 2}
    instrument = saved_instr

    correct = 0
    total = 0
    started_at = datetime.now().isoformat()
    current_root = None
    current_type = None
    current_inv = None
    feedback = ""
    feedback_attr = curses.A_NORMAL

    # Answer state
    type_options = list(ALL_CHORD_TYPES)
    inv_options = list(INVERSION_NAMES)
    answer_type_idx = 0
    answer_inv_idx = 0
    focus = 0  # 0 = quality, 1 = inversion

    BOX_W = 50
    BOX_H = 13

    def instrument_label():
        if instrument is None:
            return "Synth"
        return INSTRUMENT_NAMES[instrument]

    def pick_chord():
        nonlocal current_root, current_type, current_inv
        # Pick a valid chord type
        valid_types = [ct for ct in ALL_CHORD_TYPES if ct in enabled_chords]
        if not valid_types:
            valid_types = ["Maj"]
        current_type = random.choice(valid_types)

        # Pick a valid inversion
        max_inv = max_inversion_for(current_type)
        valid_invs = [i for i in enabled_inversions if i <= max_inv]
        if not valid_invs:
            valid_invs = [0]
        current_inv = random.choice(valid_invs)

        # Pick a root that keeps all chord notes in instrument range
        intervals = list(CHORD_INTERVALS[current_type])
        if current_inv > 0:
            for _ in range(current_inv):
                intervals.append(intervals.pop(0) + 12)
        max_interval = max(intervals)

        inst_lo, inst_hi = instrument_range(instrument)
        lo = max(inst_lo, 36)  # at least C2 for audibility
        hi = inst_hi - max_interval
        if hi < lo:
            hi = lo
        current_root = random.randint(lo, hi)

    def play_current():
        notes = build_chord_midi(current_root, current_type, current_inv)
        path = generate_chord_wav(notes, instrument)
        play_wav_async(path)

    def play_arpeggio():
        notes = build_chord_midi(current_root, current_type, current_inv)
        path = generate_arpeggio_wav(notes, instrument, note_duration=0.4)
        play_wav_async(path)

    def check_answer():
        nonlocal correct, total, feedback, feedback_attr
        answer_type = type_options[answer_type_idx]
        answer_inv = answer_inv_idx
        total += 1
        root_name = midi_to_name(current_root)
        actual_str = f"{root_name} {current_type} ({INVERSION_NAMES[current_inv]})"

        type_correct = (answer_type == current_type)
        inv_correct = (answer_inv == current_inv)

        if type_correct and inv_correct:
            correct += 1
            feedback = f"  \u2713 Correct! {actual_str}"
        else:
            ans_str = f"{answer_type}, {INVERSION_NAMES[answer_inv]}"
            feedback = f"  \u2717 Wrong! {actual_str} (you: {ans_str})"
        feedback_attr = curses.A_BOLD
        return True

    def render():
        stdscr.erase()
        y0, x0 = 1, 2

        if total == 0:
            score_str = "Score: 0/0 -%"
        else:
            pct = int(100 * correct / total)
            score_str = f"Score: {correct}/{total} {pct}%"

        draw_box(stdscr, y0, x0, BOX_H, BOX_W)
        draw_separator(stdscr, y0 + 2, x0, BOX_W)

        safe_addstr(stdscr, y0 + 1, x0 + 2, "CHORD TRAINER", curses.A_BOLD)
        safe_addstr(stdscr, y0 + 1, x0 + BOX_W - 2 - len(score_str), score_str)

        row = y0 + 3
        if feedback:
            safe_addstr(stdscr, row, x0 + 2, feedback[:BOX_W - 4], feedback_attr)
            row += 1

        safe_addstr(stdscr, row, x0 + 2, f"\u266a Listen...  ({instrument_label()})")
        row += 2

        # Quality selector
        q_label = f"[ {type_options[answer_type_idx]:<4} ]"
        q_attr = curses.A_BOLD if focus == 0 else curses.A_NORMAL
        safe_addstr(stdscr, row, x0 + 2, "Quality: ", curses.A_NORMAL)
        safe_addstr(stdscr, row, x0 + 11, q_label, q_attr)

        # Inversion selector
        i_label = f"[ {inv_options[answer_inv_idx]:<4} ]"
        i_attr = curses.A_BOLD if focus == 1 else curses.A_NORMAL
        safe_addstr(stdscr, row, x0 + 24, "Inversion: ", curses.A_NORMAL)
        safe_addstr(stdscr, row, x0 + 35, i_label, i_attr)

        safe_addstr(stdscr, y0 + BOX_H - 5, x0 + 2, "[Tab] field  [j/\u2193 k/\u2191] cycle  [Enter] submit")
        safe_addstr(stdscr, y0 + BOX_H - 4, x0 + 2, "[R] replay  [A] arpeggio  [N] chords")
        safe_addstr(stdscr, y0 + BOX_H - 3, x0 + 2, "[I] instrument")
        safe_addstr(stdscr, y0 + BOX_H - 2, x0 + 2, "[Esc] back to menu")
        stdscr.refresh()

    pick_chord()
    play_current()
    render()

    while True:
        key = stdscr.getch()

        if key == 27:
            save_chord_training_config(enabled_chords, enabled_inversions, instrument)
            if total > 0:
                save_session(
                    "chord", started_at, datetime.now().isoformat(),
                    correct, total, instrument,
                    {
                        "enabled_chords": sorted(enabled_chords),
                        "enabled_inversions": sorted(enabled_inversions),
                    },
                )
            return
        elif key in (ord("n"), ord("N")):
            enabled_chords, enabled_inversions = chord_selection_screen(
                stdscr, enabled_chords, enabled_inversions
            )
            save_chord_training_config(enabled_chords, enabled_inversions, instrument)
            feedback = ""
            answer_type_idx = 0
            answer_inv_idx = 0
            pick_chord()
            play_current()
        elif key in (ord("i"), ord("I")):
            instrument = instrument_selection_screen(stdscr, instrument)
            save_chord_training_config(enabled_chords, enabled_inversions, instrument)
            feedback = ""
            pick_chord()
            play_current()
        elif key == ord("\t"):
            focus = (focus + 1) % 2
        elif is_up(key):
            if focus == 0:
                answer_type_idx = (answer_type_idx - 1) % len(type_options)
            else:
                answer_inv_idx = (answer_inv_idx - 1) % len(inv_options)
        elif is_down(key):
            if focus == 0:
                answer_type_idx = (answer_type_idx + 1) % len(type_options)
            else:
                answer_inv_idx = (answer_inv_idx + 1) % len(inv_options)
        elif key in (ord("\n"), ord("\r")):
            check_answer()
            render()
            stdscr.refresh()
            curses.napms(1200)
            answer_type_idx = 0
            answer_inv_idx = 0
            pick_chord()
            play_current()
        elif key in (ord("r"), ord("R")):
            play_current()
        elif key in (ord("a"), ord("A")):
            play_arpeggio()

        render()

# Each progression is (name, [(semitone_offset_from_root, chord_quality), ...])
MAJOR_PROGRESSIONS = [
    ("I-IV-V-I",   [(0, "Maj"), (5, "Maj"), (7, "Maj"), (0, "Maj")]),
    ("I-vi-IV-V",  [(0, "Maj"), (9, "Min"), (5, "Maj"), (7, "Maj")]),
    ("I-V-vi-IV",  [(0, "Maj"), (7, "Maj"), (9, "Min"), (5, "Maj")]),
    ("ii-V-I",     [(2, "Min"), (7, "Maj"), (0, "Maj")]),
]

MINOR_PROGRESSIONS = [
    ("i-iv-V-i",      [(0, "Min"), (5, "Min"), (7, "Maj"), (0, "Min")]),
    ("i-VI-III-VII",  [(0, "Min"), (8, "Maj"), (3, "Maj"), (10, "Maj")]),
    ("i-iv-v-i",      [(0, "Min"), (5, "Min"), (7, "Min"), (0, "Min")]),
]

ALL_PROGRESSIONS = (
    [("major", name, chords) for name, chords in MAJOR_PROGRESSIONS]
    + [("minor", name, chords) for name, chords in MINOR_PROGRESSIONS]
)


def build_progression_midi_notes(root_midi, chord_defs):
    """Build a list of chord MIDI note lists for a progression.

    Each chord gets a bass note (root of chord in octave 2-3) and a triad in octave 4.
    """
    chords = []
    for semitone, quality in chord_defs:
        chord_root = root_midi + semitone
        # Bass note in low register (octave 2-3 range, MIDI 36-59)
        bass = chord_root
        while bass < 36:
            bass += 12
        while bass >= 60:
            bass -= 12

        # Triad in octave 4 range (MIDI 60-72 area)
        triad_root = chord_root
        while triad_root < 60:
            triad_root += 12
        while triad_root >= 72:
            triad_root -= 12

        intervals = CHORD_INTERVALS[quality]
        triad = [triad_root + iv for iv in intervals]
        chords.append([bass] + triad)

    return chords


# ── Progression selection screen ───────────────────────────────────────

def progression_selection_screen(stdscr, enabled_progressions):
    """Select which progressions to include. enabled_progressions is a set of names.
    Returns updated set, or original if cancelled."""
    BOX_W = 50
    BOX_H = 16

    selected = set(enabled_progressions)
    items = []  # (category, name)
    for cat, name, _ in ALL_PROGRESSIONS:
        items.append((cat, name))

    cursor = 0

    while True:
        stdscr.erase()
        y0, x0 = 1, 2
        draw_box(stdscr, y0, x0, BOX_H, BOX_W)
        draw_separator(stdscr, y0 + 2, x0, BOX_W)
        safe_addstr(stdscr, y0 + 1, x0 + 2, "PROGRESSION SELECTION", curses.A_BOLD)

        # Major
        safe_addstr(stdscr, y0 + 3, x0 + 3, "Major:", curses.A_NORMAL)
        row = y0 + 4
        for i, (cat, name, _) in enumerate(ALL_PROGRESSIONS):
            if cat != "major":
                continue
            check = "x" if name in selected else " "
            marker = ">" if cursor == i else " "
            attr = curses.A_BOLD if cursor == i else curses.A_NORMAL
            safe_addstr(stdscr, row, x0 + 3, f"{marker}[{check}] {name}", attr)
            row += 1

        # Minor
        row += 1
        safe_addstr(stdscr, row, x0 + 3, "Minor:", curses.A_NORMAL)
        row += 1
        for i, (cat, name, _) in enumerate(ALL_PROGRESSIONS):
            if cat != "minor":
                continue
            check = "x" if name in selected else " "
            marker = ">" if cursor == i else " "
            attr = curses.A_BOLD if cursor == i else curses.A_NORMAL
            safe_addstr(stdscr, row, x0 + 3, f"{marker}[{check}] {name}", attr)
            row += 1

        safe_addstr(stdscr, y0 + BOX_H - 3, x0 + 2, "[j/\u2193 k/\u2191] move  [Space] toggle")
        safe_addstr(stdscr, y0 + BOX_H - 2, x0 + 2, "[Enter] confirm  [Esc] cancel")
        stdscr.refresh()

        key = stdscr.getch()
        if key == 27:
            return enabled_progressions
        elif is_down(key):
            cursor = (cursor + 1) % len(items)
        elif is_up(key):
            cursor = (cursor - 1) % len(items)
        elif key == ord(" "):
            _, name = items[cursor]
            if name in selected:
                if len(selected) > 1:
                    selected.discard(name)
            else:
                selected.add(name)
        elif key in (ord("\n"), ord("\r")):
            if len(selected) >= 1:
                return selected


# ── Key training mode ──────────────────────────────────────────────────

def key_training(stdscr):
    """Key identification ear training loop."""
    saved_progs, saved_instr = load_key_training_config()
    enabled_progressions = saved_progs if saved_progs is not None else {name for _, name, _ in ALL_PROGRESSIONS}
    instrument = saved_instr

    correct = 0
    total = 0
    started_at = datetime.now().isoformat()
    current_root_pc = None  # pitch class 0-11
    current_quality = None  # "major" or "minor"
    current_prog_name = None
    feedback = ""
    feedback_attr = curses.A_NORMAL

    answer_letter = ""
    accidental_idx = 0
    answer_quality = 0  # 0=Major, 1=Minor

    BOX_W = 50
    BOX_H = 14

    def pick_progression():
        nonlocal current_root_pc, current_quality, current_prog_name
        # Filter to enabled progressions
        valid = [(cat, name, chords) for cat, name, chords in ALL_PROGRESSIONS
                 if name in enabled_progressions]
        if not valid:
            valid = ALL_PROGRESSIONS[:1]

        cat, name, chord_defs = random.choice(valid)
        current_quality = cat
        current_prog_name = name
        current_root_pc = random.randint(0, 11)

    def play_current():
        # Find the progression chord_defs
        chord_defs = None
        for cat, name, chords in ALL_PROGRESSIONS:
            if name == current_prog_name:
                chord_defs = chords
                break

        # Build MIDI root in a reasonable range (C3-C4 = MIDI 48-60)
        root_midi = 48 + current_root_pc
        chord_sequence = build_progression_midi_notes(root_midi, chord_defs)
        path = generate_progression_wav(chord_sequence, instrument, chord_duration=1.0)
        play_wav_async(path)

    def get_answer_str():
        if not answer_letter:
            return "_"
        return answer_letter + ACCIDENTAL_CYCLE[accidental_idx]

    def get_quality_str():
        return "Major" if answer_quality == 0 else "Minor"

    def check_answer():
        nonlocal correct, total, feedback, feedback_attr
        answer = answer_letter + ACCIDENTAL_CYCLE[accidental_idx]
        if answer not in NAME_TO_PITCH_CLASS:
            feedback = "  Invalid note: " + answer
            feedback_attr = curses.A_BOLD
            return False

        answer_pc = NAME_TO_PITCH_CLASS[answer]
        answer_q = "major" if answer_quality == 0 else "minor"
        total += 1

        actual_name = NOTE_NAMES[current_root_pc]
        actual_str = f"{actual_name} {current_quality} ({current_prog_name})"

        if answer_pc == current_root_pc and answer_q == current_quality:
            correct += 1
            feedback = f"  \u2713 Correct! {actual_str}"
        else:
            ans_q = "major" if answer_quality == 0 else "minor"
            feedback = f"  \u2717 Wrong! {actual_str} (you: {answer} {ans_q})"
        feedback_attr = curses.A_BOLD
        return True

    def instrument_label():
        if instrument is None:
            return "Synth"
        return INSTRUMENT_NAMES[instrument]

    def render():
        stdscr.erase()
        y0, x0 = 1, 2

        if total == 0:
            score_str = "Score: 0/0 -%"
        else:
            pct = int(100 * correct / total)
            score_str = f"Score: {correct}/{total} {pct}%"

        draw_box(stdscr, y0, x0, BOX_H, BOX_W)
        draw_separator(stdscr, y0 + 2, x0, BOX_W)

        safe_addstr(stdscr, y0 + 1, x0 + 2, "KEY TRAINER", curses.A_BOLD)
        safe_addstr(stdscr, y0 + 1, x0 + BOX_W - 2 - len(score_str), score_str)

        row = y0 + 3
        if feedback:
            safe_addstr(stdscr, row, x0 + 2, feedback[:BOX_W - 4], feedback_attr)
            row += 1

        safe_addstr(stdscr, row, x0 + 2, f"\u266a Listen...  ({instrument_label()})")
        row += 2

        # Key answer
        safe_addstr(stdscr, row, x0 + 2, f"Key: {get_answer_str():<4}")
        # Quality answer
        q_str = f"[ {get_quality_str():<5} ]"
        safe_addstr(stdscr, row, x0 + 16, "Quality: ")
        safe_addstr(stdscr, row, x0 + 25, q_str, curses.A_BOLD)

        safe_addstr(stdscr, y0 + BOX_H - 5, x0 + 2, "[A-G] key  [Tab] #/b  [Space] Maj/Min")
        safe_addstr(stdscr, y0 + BOX_H - 4, x0 + 2, "[R] replay  [N] progressions")
        safe_addstr(stdscr, y0 + BOX_H - 3, x0 + 2, "[I] instrument  [Enter] submit")
        safe_addstr(stdscr, y0 + BOX_H - 2, x0 + 2, "[Esc] back to menu")
        stdscr.refresh()

    pick_progression()
    play_current()
    render()

    while True:
        key = stdscr.getch()

        if key == 27:
            save_key_training_config(enabled_progressions, instrument)
            if total > 0:
                save_session(
                    "key", started_at, datetime.now().isoformat(),
                    correct, total, instrument,
                    {"enabled_progressions": sorted(enabled_progressions)},
                )
            return
        elif key in (ord("n"), ord("N")):
            enabled_progressions = progression_selection_screen(
                stdscr, enabled_progressions
            )
            save_key_training_config(enabled_progressions, instrument)
            feedback = ""
            answer_letter = ""
            accidental_idx = 0
            pick_progression()
            play_current()
        elif key in (ord("i"), ord("I")):
            instrument = instrument_selection_screen(stdscr, instrument)
            save_key_training_config(enabled_progressions, instrument)
            feedback = ""
            pick_progression()
            play_current()
        elif key == ord("\t"):
            if answer_letter:
                accidental_idx = (accidental_idx + 1) % 3
        elif key == ord(" "):
            answer_quality = (answer_quality + 1) % 2
        elif key in (ord("\n"), ord("\r")):
            if answer_letter:
                if check_answer():
                    render()
                    stdscr.refresh()
                    curses.napms(1500)
                    answer_letter = ""
                    accidental_idx = 0
                    pick_progression()
                    play_current()
        elif key in (ord("r"), ord("R")):
            play_current()
        elif ord("a") <= key <= ord("g") or ord("A") <= key <= ord("G"):
            answer_letter = chr(key).upper()
            accidental_idx = 0

        render()


# ── Main ───────────────────────────────────────────────────────────────

def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.keypad(True)

    while True:
        mode = mode_select_screen(stdscr)
        if mode is None:
            break
        elif mode == "note":
            note_training(stdscr)
        elif mode == "chord":
            chord_training(stdscr)
        elif mode == "key":
            key_training(stdscr)


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass

    print("\nThanks for practicing!")
