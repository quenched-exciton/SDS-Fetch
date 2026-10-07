"""
gui.py - The single-window program the user sees.

Layout, from top to bottom:
    1. Chemicals      - the list (one CAS number per line, optional "@ manufacturer"),
                        buttons to load a CSV/Excel file or the failed numbers of
                        the last run, and a live check of the list below the box
    2. Manufacturer   - preferred manufacturers + "strict" tick box (both optional)
    3. Download folder- folder picker + "Overwrite existing PDFs" tick box
    Run / Stop buttons
    Progress          - status line, progress bar and a running message log

Why a background thread?
    Downloading takes a while. If the download ran in the same thread as the
    window, the window would freeze ("Not responding") until it finished. So the
    download runs in a separate "worker" thread. Tkinter windows may only be
    changed from the main thread, so the worker never touches the window
    directly: it puts messages into a queue, and the window checks that queue
    every 100 milliseconds (see _check_queue).
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .cas import ParsedInput, parse_text_list, read_table_rows, rows_to_lines
from .downloader import BatchResult, run_batch
from .settings import load_settings, save_settings

# Background colours for lines in the list (see _check_list).
PROBLEM_COLOUR = "#ffd6d6"    # light red: no valid CAS number on this line
DUPLICATE_COLOUR = "#fff1c2"  # light yellow: CAS number already listed above
NOTE_COLOUR = "#7a7a7a"       # grey text: "# note"

LIST_HELP = (
    "One chemical per line, e.g. 7758-99-8. To ask for one manufacturer's SDS, add @ and its name: "
    "2530-83-8 @ Gelest. Text after # is a note. Chemical names pasted next to the CAS number "
    "are ignored. Ctrl+Enter starts the run."
)


class SdsFetchApp:
    """The main window. Create it with a Tk root window, then call root.mainloop()."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("SDS Fetch")
        root.minsize(680, 700)

        settings = load_settings()

        # "Tk variables" are linked to widgets: when the user types or clicks,
        # the variable changes, and when we change the variable, the widget updates.
        self.download_folder = tk.StringVar(value=settings["download_folder"])
        self.preferred = tk.StringVar(value=settings["preferred_manufacturers"])
        self.strict = tk.BooleanVar(value=settings["strict"])
        self.overwrite = tk.BooleanVar(value=settings["overwrite"])
        self.check_text = tk.StringVar()
        self.status_text = tk.StringVar(value="Ready. Enter CAS numbers, choose a folder, then press Run.")

        self.loaded_from = ""                 # file the list was loaded from, for the log
        self.last_failed_lines: list[str] = []  # for "Failed from last run"
        self._check_pending = None            # timer id of the delayed list check

        # Communication with the worker thread (see the module description).
        self.messages: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

        self._build_widgets()
        if settings["chemical_list"]:
            self.cas_textbox.insert("1.0", settings["chemical_list"])
            self.cas_textbox.edit_reset()  # so Ctrl+Z cannot undo the restored list away
        self._check_list()
        self.cas_textbox.focus_set()
        # Save the settings when the window is closed with the X button.
        root.protocol("WM_DELETE_WINDOW", self._close)

    # ------------------------------------------------------------------
    # Building the window
    # ------------------------------------------------------------------

    def _build_widgets(self) -> None:
        # One outer frame with padding; everything else goes inside it.
        outer = ttk.Frame(self.root, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        # --- 1. Chemicals -------------------------------------------------------
        list_frame = ttk.LabelFrame(outer, text="1. Chemicals", padding=8)
        list_frame.grid(row=0, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(2, weight=1)

        buttons = ttk.Frame(list_frame)
        buttons.grid(row=0, column=0, sticky="ew")
        self.load_button = ttk.Button(buttons, text="Load CSV / Excel file...", command=self._load_file)
        self.load_button.grid(row=0, column=0, padx=(0, 6))
        self.failed_button = ttk.Button(buttons, text="Failed from last run", state="disabled",
                                        command=self._load_failed)
        self.failed_button.grid(row=0, column=1, padx=(0, 6))
        self.clear_button = ttk.Button(buttons, text="Clear", command=self._clear_list)
        self.clear_button.grid(row=0, column=2)

        ttk.Label(list_frame, text=LIST_HELP, wraplength=620, justify="left").grid(
            row=1, column=0, sticky="w", pady=(6, 4))
        self.cas_textbox = ScrolledText(list_frame, height=10, width=60, wrap="none", undo=True)
        self.cas_textbox.grid(row=2, column=0, sticky="nsew")
        self.cas_textbox.tag_configure("problem", background=PROBLEM_COLOUR)
        self.cas_textbox.tag_configure("duplicate", background=DUPLICATE_COLOUR)
        self.cas_textbox.tag_configure("note", foreground=NOTE_COLOUR)
        # "<<Modified>>" fires when the text changes; the list is checked shortly after.
        self.cas_textbox.bind("<<Modified>>", self._list_changed)
        self.cas_textbox.bind("<Control-Return>", self._run_from_keyboard)

        self.check_label = ttk.Label(list_frame, textvariable=self.check_text, wraplength=620, justify="left")
        self.check_label.grid(row=3, column=0, sticky="w", pady=(4, 0))

        # --- 2. Manufacturer -------------------------------------------------------
        maker_frame = ttk.LabelFrame(outer, text="2. Manufacturer (optional)", padding=8)
        maker_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        maker_frame.columnconfigure(1, weight=1)
        ttk.Label(maker_frame, text="Preferred:").grid(row=0, column=0, sticky="w")
        self.preferred_entry = ttk.Entry(maker_frame, textvariable=self.preferred)
        self.preferred_entry.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Label(maker_frame,
                  text="For lines without @. Most wanted first, separated by commas, e.g. "
                       "\"Sigma-Aldrich, Thermo Fisher\". Brand names count as their company "
                       "(Merck, Aldrich, Fluka = Sigma-Aldrich; Acros, Alfa Aesar = Thermo Fisher). "
                       "When no requested manufacturer has an SDS, another one's is saved, unless "
                       "Strict is ticked.",
                  wraplength=620, justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.strict_check = ttk.Checkbutton(
            maker_frame, variable=self.strict,
            text="Strict: only save SDS files from a requested manufacturer")
        self.strict_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # --- 3. Download folder ---------------------------------------------
        folder_frame = ttk.LabelFrame(outer, text="3. Download folder", padding=8)
        folder_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        folder_frame.columnconfigure(0, weight=1)
        self.folder_entry = ttk.Entry(folder_frame, textvariable=self.download_folder)
        self.folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.folder_button = ttk.Button(folder_frame, text="Browse...", command=self._browse_folder)
        self.folder_button.grid(row=0, column=1)
        self.overwrite_check = ttk.Checkbutton(
            folder_frame, text="Overwrite PDFs that already exist in this folder", variable=self.overwrite)
        self.overwrite_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # --- Run / Stop buttons -----------------------------------------------
        button_row = ttk.Frame(outer)
        button_row.grid(row=3, column=0, sticky="ew", pady=10)
        button_row.columnconfigure(0, weight=1)
        self.stop_button = ttk.Button(button_row, text="Stop", command=self._stop, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=(0, 6))
        self.run_button = ttk.Button(button_row, text="Run", command=self._run)
        self.run_button.grid(row=0, column=2)

        # --- Progress (bottom of the window) ---------------------------------
        progress_frame = ttk.LabelFrame(outer, text="Progress", padding=8)
        progress_frame.grid(row=4, column=0, sticky="nsew")
        progress_frame.columnconfigure(0, weight=1)
        progress_frame.rowconfigure(2, weight=1)
        ttk.Label(progress_frame, textvariable=self.status_text).grid(row=0, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=6)
        self.progress_log = ScrolledText(progress_frame, height=10, width=60, state="disabled",
                                         wrap="word", font="TkFixedFont")
        self.progress_log.grid(row=2, column=0, sticky="nsew")

        # Let the two text areas share any extra space when the window is enlarged.
        outer.rowconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

    # ------------------------------------------------------------------
    # The chemical list: live check, loading, clearing
    # ------------------------------------------------------------------

    def _list_text(self) -> str:
        return self.cas_textbox.get("1.0", "end-1c")  # "end-1c" leaves out Tk's extra final newline

    def _list_changed(self, _event=None) -> None:
        """Called on every change of the list. Checks it 0.4 s after typing stops."""
        self.cas_textbox.edit_modified(False)  # re-arm, or <<Modified>> fires only once
        if self._check_pending is not None:
            self.root.after_cancel(self._check_pending)
        self._check_pending = self.root.after(400, self._check_list)

    def _check_list(self) -> ParsedInput:
        """
        Check the list, colour the lines and show a summary below the box:
        light red = no valid CAS number, light yellow = repeated, grey = note.
        """
        self._check_pending = None
        text = self._list_text()
        parsed = parse_text_list(text)

        for tag in ("problem", "duplicate", "note"):
            self.cas_textbox.tag_remove(tag, "1.0", "end")
        for entry in parsed.invalid:
            self.cas_textbox.tag_add("problem", f"{entry.line}.0", f"{entry.line}.end")
        for line in parsed.duplicate_lines:
            self.cas_textbox.tag_add("duplicate", f"{line}.0", f"{line}.end")
        for number, line in enumerate(text.splitlines(), start=1):
            if "#" in line:
                self.cas_textbox.tag_add("note", f"{number}.{line.index('#')}", f"{number}.end")

        # Summary line below the box.
        if not parsed.valid_cas and not parsed.invalid:
            self.check_text.set("The list is empty.")
            self.check_label.configure(foreground="")
            return parsed
        summary = f"{len(parsed.valid_cas)} CAS number(s) ready"
        if parsed.manufacturers:
            summary += f", {len(parsed.manufacturers)} with a manufacturer"
        summary += "."
        if parsed.duplicates_ignored:
            summary += f" {parsed.duplicates_ignored} repeat(s) will be skipped (yellow)."
        if parsed.invalid:
            first = parsed.invalid[0]
            summary += (f" {len(parsed.invalid)} problem(s) in red - line {first.line}: "
                        f"\"{first.text}\" - {first.reason}.")
        self.check_text.set(summary)
        self.check_label.configure(foreground="#b00020" if parsed.invalid else "#1b6e20")
        return parsed

    def _replace_or_add(self, lines: list[str], what: str) -> bool:
        """Put `lines` in the list, asking first when the list is not empty. False = cancelled."""
        new_text = "\n".join(lines)
        if self._list_text().strip():
            answer = messagebox.askyesnocancel(
                what, f"Replace the current list with the {len(lines)} new line(s)?\n\n"
                      "Yes = replace,  No = add them at the end,  Cancel = do nothing.\n"
                      "(Ctrl+Z in the list undoes this.)")
            if answer is None:
                return False
            if answer is False:
                existing = self._list_text().rstrip("\n")
                self.cas_textbox.insert("end", ("\n" if existing else "") + new_text)
                self._check_list()
                return True
        self.cas_textbox.delete("1.0", "end")
        self.cas_textbox.insert("1.0", new_text)
        self._check_list()
        return True

    def _load_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a CSV or Excel file with CAS numbers",
            filetypes=[("CSV or Excel files", "*.csv *.xlsx *.xlsm *.txt"), ("All files", "*.*")],
        )
        if not path:  # empty when the user pressed Cancel
            return
        try:
            lines = rows_to_lines(read_table_rows(path))
        except Exception as error:  # damaged file, wrong format, missing openpyxl, ...
            messagebox.showerror("Could not read the file", str(error))
            return
        if not lines:
            messagebox.showwarning("Empty file", "No rows with content were found in this file.")
            return
        if self._replace_or_add(lines, "Load file"):
            self.loaded_from = path
            self.status_text.set(f"Loaded {len(lines)} line(s) from {Path(path).name}. "
                                 "Check the list, then press Run.")

    def _load_failed(self) -> None:
        if not self.last_failed_lines:
            return  # nothing failed (the button is normally greyed out then)
        if self._replace_or_add(self.last_failed_lines, "Failed from last run"):
            self.status_text.set(f"{len(self.last_failed_lines)} CAS number(s) from the last run "
                                 "that got no SDS. Press Run to try them again.")

    def _clear_list(self) -> None:
        self.cas_textbox.delete("1.0", "end")  # Ctrl+Z brings it back
        self.loaded_from = ""
        self._check_list()

    # ------------------------------------------------------------------
    # Small helpers used by the buttons
    # ------------------------------------------------------------------

    def _browse_folder(self) -> None:
        path = filedialog.askdirectory(title="Choose the folder for the SDS files")
        if path:
            self.download_folder.set(path)

    def _append_progress(self, message: str) -> None:
        """Add one line to the progress box and scroll to the bottom."""
        self.progress_log.configure(state="normal")   # must be "normal" to insert text
        self.progress_log.insert("end", message + "\n")
        self.progress_log.see("end")
        self.progress_log.configure(state="disabled")  # read-only again for the user

    def _set_inputs_enabled(self, enabled: bool) -> None:
        """Lock the inputs while a download is running, unlock them afterwards."""
        state = "normal" if enabled else "disabled"
        for widget in (self.load_button, self.clear_button, self.preferred_entry, self.strict_check,
                       self.folder_entry, self.folder_button, self.overwrite_check, self.run_button):
            widget.configure(state=state)
        self.failed_button.configure(state="normal" if enabled and self.last_failed_lines else "disabled")
        self.cas_textbox.configure(state=state)
        self.stop_button.configure(state="disabled" if enabled else "normal")

    def _preferred_list(self) -> list[str]:
        """The "Preferred" box as a list: "Sigma, Thermo Fisher" -> ["Sigma", "Thermo Fisher"]."""
        return [name.strip() for name in re.split(r"[,;]", self.preferred.get()) if name.strip()]

    def _save_settings(self) -> None:
        save_settings({
            "download_folder": self.download_folder.get(),
            "preferred_manufacturers": self.preferred.get(),
            "strict": self.strict.get(),
            "overwrite": self.overwrite.get(),
            "chemical_list": self._list_text(),
        })

    def _close(self) -> None:
        self._save_settings()
        self.root.destroy()

    # ------------------------------------------------------------------
    # Run: check the inputs, then start the worker thread
    # ------------------------------------------------------------------

    def _check_folder(self) -> Path | None:
        """Make sure a usable download folder is chosen. Returns None on a problem."""
        folder_text = self.download_folder.get().strip()
        if not folder_text:
            messagebox.showwarning("No folder", "Please choose a download folder.")
            return None
        folder = Path(folder_text).expanduser()
        if folder.exists() and not folder.is_dir():
            messagebox.showerror("Not a folder", f"{folder}\nis a file, not a folder.")
            return None
        if not folder.exists():
            if not messagebox.askyesno("Create folder?", f"The folder\n{folder}\ndoes not exist. Create it?"):
                return None
            try:
                folder.mkdir(parents=True)
            except OSError as error:
                messagebox.showerror("Could not create the folder", str(error))
                return None
        return folder

    def _run_from_keyboard(self, _event=None) -> str:
        self._run()
        return "break"  # stops Tk from also inserting a new line

    def _run(self) -> None:
        """Called when the Run button (or Ctrl+Enter) is pressed."""
        if str(self.run_button.cget("state")) == "disabled":
            return  # a run is already going
        parsed = self._check_list()

        if not parsed.valid_cas:
            if not parsed.invalid:
                messagebox.showwarning("No CAS numbers", "Please type, paste or load at least one CAS number.")
            else:
                examples = "\n".join(f"  line {e.line}: {e.text} - {e.reason}" for e in parsed.invalid[:10])
                messagebox.showerror("No valid CAS numbers",
                                     "None of the lines has a valid CAS number.\n\n" + examples)
            return
        if parsed.invalid and not messagebox.askyesno(
                "Problems in the list",
                f"{len(parsed.invalid)} entr{'y has' if len(parsed.invalid) == 1 else 'ies have'} a problem "
                "(highlighted in red) and will be skipped:\n\n"
                + "\n".join(f"  line {e.line}: {e.text} - {e.reason}" for e in parsed.invalid[:8])
                + ("\n  ..." if len(parsed.invalid) > 8 else "")
                + f"\n\nRun anyway with the {len(parsed.valid_cas)} valid CAS number(s)?"):
            return

        folder = self._check_folder()
        if folder is None:
            return
        self._save_settings()

        input_description = "list in the window"
        if self.loaded_from:
            input_description += f" (loaded from {self.loaded_from})"
        preferred = self._preferred_list()

        # Reset the progress area for a fresh run.
        self.progress_log.configure(state="normal")
        self.progress_log.delete("1.0", "end")
        self.progress_log.configure(state="disabled")
        self.progress_bar.configure(value=0, maximum=len(parsed.valid_cas))
        self.status_text.set("Starting...")
        self.stop_event.clear()
        self._set_inputs_enabled(False)

        # daemon=True: if the user closes the window, the worker thread ends too.
        self.worker = threading.Thread(
            target=self._worker,
            args=(parsed, folder, input_description, self.overwrite.get(), preferred, self.strict.get()),
            daemon=True)
        self.worker.start()
        self.root.after(100, self._check_queue)

    def _stop(self) -> None:
        """Called when the Stop button is pressed."""
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self.status_text.set("Stopping after the current chemical...")

    # ------------------------------------------------------------------
    # The worker thread and the message queue
    # ------------------------------------------------------------------

    def _worker(self, parsed: ParsedInput, folder: Path, input_description: str, overwrite: bool,
                preferred: list[str], strict: bool) -> None:
        """Runs in the background thread. Only talks to the window through the queue."""
        try:
            batch = run_batch(
                parsed,
                folder,
                input_description,
                overwrite=overwrite,
                report=lambda message: self.messages.put(("log", message)),
                progress=lambda done, total: self.messages.put(("progress", (done, total))),
                stop_event=self.stop_event,
                preferred=preferred,
                strict=strict,
            )
            self.messages.put(("done", (batch, parsed)))
        except Exception:
            self.messages.put(("error", traceback.format_exc()))

    def _check_queue(self) -> None:
        """Runs in the main thread every 100 ms: moves worker messages onto the screen."""
        try:
            while True:
                kind, data = self.messages.get_nowait()
                if kind == "log":
                    self._append_progress(data)
                elif kind == "progress":
                    done, total = data
                    self.progress_bar.configure(value=done, maximum=max(total, 1))
                    self.status_text.set(f"Processed {done} of {total} CAS number(s)...")
                elif kind == "done":
                    self._finished(*data)
                    return  # stop polling; the run is over
                elif kind == "error":
                    self._append_progress(data)
                    self.status_text.set("Stopped because of an unexpected error (see below).")
                    self._set_inputs_enabled(True)
                    messagebox.showerror("Unexpected error", data.strip().splitlines()[-1])
                    return
        except queue.Empty:
            pass  # no more messages right now
        self.root.after(100, self._check_queue)

    def _finished(self, batch: BatchResult, parsed: ParsedInput) -> None:
        """Show the summary when the worker is done."""
        # Remember what got no SDS (keeping any "@ manufacturer") for "Failed from last run".
        retry = [r.cas for r in batch.failures] + list(batch.not_processed)
        self.last_failed_lines = [
            f"{cas} @ {parsed.manufacturers[cas]}" if cas in parsed.manufacturers else cas for cas in retry]

        self._set_inputs_enabled(True)
        summary = (f"{len(batch.successes)} successful, {len(batch.failures)} failed, "
                   f"{len(parsed.invalid)} invalid")
        if batch.cancelled:
            summary += f", {len(batch.not_processed)} not processed (stopped)"
        self.status_text.set(("Stopped: " if batch.cancelled else "Finished: ") + summary + "."
                             + (" 'Failed from last run' loads the rest." if retry else ""))

        if messagebox.askyesno("SDS download finished",
                               f"{summary}.\n\nLog file:\n{batch.log_path}\n\nHazard summary (opens in Excel):\n"
                               f"{batch.summary_path}\n\nOpen the download folder now?"):
            open_folder(batch.log_path.parent)


def open_folder(folder: Path) -> None:
    """Open a folder in Windows Explorer / macOS Finder / the Linux file manager."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(folder)  # type: ignore[attr-defined]  (exists on Windows only)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception as error:
        messagebox.showinfo("Download folder", f"Could not open the folder automatically:\n{folder}\n\n{error}")


def main() -> None:
    """Start the program."""
    # On Windows with display scaling (125 %, 150 %...), this stops the window
    # from looking blurry. It does nothing on macOS or Linux.
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    SdsFetchApp(root)
    root.mainloop()
