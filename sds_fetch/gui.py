"""
gui.py - The single-window program the user sees.

Layout, from top to bottom:
    1. Input method   - radio buttons: "Type or paste a list" / "Load a CSV file"
    2. CAS numbers    - EITHER a multi-line text box OR a CSV file picker
                        (switches automatically when the radio button changes)
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
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .cas import ParsedInput, parse_csv_file, parse_text_list
from .downloader import BatchResult, run_batch


class SdsFetchApp:
    """The main window. Create it with a Tk root window, then call root.mainloop()."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("SDS Fetch")
        root.minsize(640, 640)

        # "Tk variables" are linked to widgets: when the user types or clicks,
        # the variable changes, and when we change the variable, the widget updates.
        self.input_mode = tk.StringVar(value="text")    # "text" or "csv"
        self.csv_path = tk.StringVar()
        self.download_folder = tk.StringVar()
        self.overwrite = tk.BooleanVar(value=False)
        self.status_text = tk.StringVar(value="Ready. Enter CAS numbers, choose a folder, then press Run.")

        # Communication with the worker thread (see the module description).
        self.messages: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

        self._build_widgets()
        self._show_input_panel()  # show the correct panel for the default choice

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

        # --- 1. Input method -------------------------------------------------
        mode_frame = ttk.LabelFrame(outer, text="1. How will you provide the CAS numbers?", padding=8)
        mode_frame.grid(row=0, column=0, sticky="ew")
        # "command=" runs _show_input_panel every time a radio button is clicked.
        self.text_radio = ttk.Radiobutton(mode_frame, text="Type or paste a list", value="text",
                                          variable=self.input_mode, command=self._show_input_panel)
        self.csv_radio = ttk.Radiobutton(mode_frame, text="Load a CSV file", value="csv",
                                         variable=self.input_mode, command=self._show_input_panel)
        self.text_radio.grid(row=0, column=0, padx=(0, 24), sticky="w")
        self.csv_radio.grid(row=0, column=1, sticky="w")

        # --- 2. CAS numbers (two panels in the same place; only one is shown) --
        list_frame = ttk.LabelFrame(outer, text="2. CAS numbers", padding=8)
        list_frame.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        # Panel A: multi-line text box
        self.text_panel = ttk.Frame(list_frame)
        self.text_panel.columnconfigure(0, weight=1)
        self.text_panel.rowconfigure(1, weight=1)
        ttk.Label(self.text_panel,
                  text="One CAS number per line, e.g. 7758-99-8. Commas, semicolons or spaces "
                       "between numbers also work, and chemical names on the same line are ignored.",
                  wraplength=580, justify="left").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.cas_textbox = ScrolledText(self.text_panel, height=9, width=60, wrap="none", undo=True)
        self.cas_textbox.grid(row=1, column=0, sticky="nsew")

        # Panel B: CSV file picker
        self.csv_panel = ttk.Frame(list_frame)
        self.csv_panel.columnconfigure(1, weight=1)
        ttk.Label(self.csv_panel, text="CSV file:").grid(row=0, column=0, sticky="w")
        self.csv_entry = ttk.Entry(self.csv_panel, textvariable=self.csv_path)
        self.csv_entry.grid(row=0, column=1, sticky="ew", padx=6)
        self.csv_button = ttk.Button(self.csv_panel, text="Browse...", command=self._browse_csv)
        self.csv_button.grid(row=0, column=2)
        ttk.Label(self.csv_panel,
                  text="The column whose header contains \"CAS\" (e.g. \"CAS Number\") is used. "
                       "If no header contains \"CAS\", the first column is used.",
                  wraplength=580, justify="left").grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # Both panels sit in the same grid cell; _show_input_panel hides one of them.
        self.text_panel.grid(row=0, column=0, sticky="nsew")
        self.csv_panel.grid(row=0, column=0, sticky="new")

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
        self.progress_log = ScrolledText(progress_frame, height=12, width=60, state="disabled",
                                         wrap="word", font="TkFixedFont")
        self.progress_log.grid(row=2, column=0, sticky="nsew")

        # Let the two text areas share any extra space when the window is enlarged.
        outer.rowconfigure(1, weight=1)
        outer.rowconfigure(4, weight=2)

    # ------------------------------------------------------------------
    # Small helpers used by the buttons
    # ------------------------------------------------------------------

    def _show_input_panel(self) -> None:
        """Show the text box OR the CSV picker, depending on the radio button."""
        if self.input_mode.get() == "text":
            self.csv_panel.grid_remove()   # grid_remove hides a widget but remembers its place
            self.text_panel.grid()
            self.cas_textbox.focus_set()
        else:
            self.text_panel.grid_remove()
            self.csv_panel.grid()

    def _browse_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a CSV file with CAS numbers",
            filetypes=[("CSV files", "*.csv"), ("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:  # empty when the user pressed Cancel
            self.csv_path.set(path)

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
        for widget in (self.text_radio, self.csv_radio, self.csv_entry, self.csv_button,
                       self.folder_entry, self.folder_button, self.overwrite_check, self.run_button):
            widget.configure(state=state)
        self.cas_textbox.configure(state=state)
        self.stop_button.configure(state="disabled" if enabled else "normal")

    # ------------------------------------------------------------------
    # Run: check the inputs, then start the worker thread
    # ------------------------------------------------------------------

    def _read_input(self) -> tuple[ParsedInput, str] | None:
        """Read the CAS list from the text box or CSV file. Returns None on a problem."""
        if self.input_mode.get() == "text":
            text = self.cas_textbox.get("1.0", "end")
            if not text.strip():
                messagebox.showwarning("No CAS numbers", "Please type or paste at least one CAS number.")
                return None
            return parse_text_list(text), "typed/pasted list"

        csv_file = self.csv_path.get().strip()
        if not csv_file or not Path(csv_file).is_file():
            messagebox.showwarning("No CSV file", "Please choose an existing CSV file.")
            return None
        try:
            return parse_csv_file(csv_file), f"CSV file: {csv_file}"
        except Exception as error:
            messagebox.showerror("Could not read the CSV file", str(error))
            return None

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

    def _run(self) -> None:
        """Called when the Run button is pressed."""
        read = self._read_input()
        if read is None:
            return
        parsed, input_description = read

        if not parsed.valid_cas:
            examples = "\n".join(f"  {entry.text}: {entry.reason}" for entry in parsed.invalid[:10])
            messagebox.showerror("No valid CAS numbers",
                                 "None of the entries is a valid CAS number.\n\n" + examples)
            return

        folder = self._check_folder()
        if folder is None:
            return

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
            target=self._worker, args=(parsed, folder, input_description, self.overwrite.get()), daemon=True)
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

    def _worker(self, parsed: ParsedInput, folder: Path, input_description: str, overwrite: bool) -> None:
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
        self._set_inputs_enabled(True)
        summary = (f"{len(batch.successes)} successful, {len(batch.failures)} failed, "
                   f"{len(parsed.invalid)} invalid")
        if batch.cancelled:
            summary += f", {len(batch.not_processed)} not processed (stopped)"
        self.status_text.set(("Stopped: " if batch.cancelled else "Finished: ") + summary + ".")

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
