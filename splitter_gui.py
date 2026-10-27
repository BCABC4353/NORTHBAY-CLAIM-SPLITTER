"""NorthBay Claim PDF Splitter — drag-and-drop GUI."""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _HAS_TKDND = True
except ImportError:
    _HAS_TKDND = False
    TkinterDnD = None
    DND_FILES = None

from pbi_client import PbiClient, PbiError
from splitter_engine import (
    CollateResult,
    CollationError,
    EmptyPageError,
    InputTooLargeError,
    OutputCorruptError,
    OutputExistsError,
    __version__,
    collate_pdf,
)
from updater import Updater

logger = logging.getLogger(__name__)

_FONT_FAMILY = ("Segoe UI", "Helvetica", "sans-serif")

_BaseApp = TkinterDnD.Tk if _HAS_TKDND else tk.Tk  # type: ignore[union-attr]  # _HAS_TKDND guards None case


class SplitterApp(_BaseApp):  # type: ignore[misc]  # dynamic base; _BaseApp resolves at import time
    """Drag-and-drop GUI for the NorthBay Claim Splitter."""

    def __init__(self):
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                import ctypes

                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass

        if not _HAS_TKDND:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Missing Dependency",
                "tkinterdnd2 is not installed.\n\n"
                "Install it with: pip install tkinterdnd2\n\n"
                "The application cannot start without drag-and-drop support.",
            )
            raise SystemExit(1)

        super().__init__()
        self._processing = False
        self._update_pending = False
        self._client: PbiClient | None = None
        self.title(f"NorthBay Claim Splitter v{__version__}")

        try:
            scaling = self.tk.call("tk", "scaling")
            base_w = int(450 * scaling / 1.333)
            base_h = int(360 * scaling / 1.333)
            self.geometry(f"{max(450, base_w)}x{max(360, base_h)}")
        except Exception:
            self.geometry("450x360")
        self.minsize(450, 360)

        self._build_menu()
        self._build_widgets()

        self.bind("<Control-o>", lambda e: self._open_dialog())
        self.bind("<F1>", lambda e: self._show_about())
        self.drop_frame.configure(takefocus=True)
        self.drop_frame.bind("<Return>", lambda e: self._open_dialog())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        Updater(
            is_idle=lambda: not self._processing,
            on_status=lambda text: self._safe_after(0, self.status_var.set, text),
            quit_app=lambda: self._safe_after(0, self._quit_for_update),
        ).start()

    def _quit_for_update(self):
        self._update_pending = True
        if self._processing:
            self.after(1000, self._quit_for_update)
            return
        self.destroy()

    def _build_menu(self):
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open PDF...", command=self._open_dialog)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)

    def _build_widgets(self):
        self._drop_bg_default = "#e8e8e8"
        self._drop_bg_hover = "#c8daf0"
        self.drop_frame = tk.Frame(
            self,
            relief="groove",
            bd=2,
            bg=self._drop_bg_default,
            cursor="hand2",
            highlightthickness=2,
            highlightcolor="#4a90d9",
        )
        self.drop_frame.pack(padx=20, pady=(15, 10), fill="both", expand=True)

        self.drop_label = tk.Label(
            self.drop_frame,
            text="Drag NorthBay claim PDF here\n\nor click to browse",
            bg=self._drop_bg_default,
            fg="#555555",
            font=(_FONT_FAMILY[0], 12),
        )
        self.drop_label.pack(expand=True)

        self.drop_frame.drop_target_register(DND_FILES)  # type: ignore[attr-defined]  # tkinterdnd2 patches Frame
        self.drop_frame.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]
        self.drop_frame.dnd_bind("<<DragEnter>>", self._on_drag_enter)  # type: ignore[attr-defined]
        self.drop_frame.dnd_bind("<<DragLeave>>", self._on_drag_leave)  # type: ignore[attr-defined]
        self.drop_frame.bind("<Button-1>", lambda e: self._open_dialog())
        self.drop_label.bind("<Button-1>", lambda e: self._open_dialog())

        self.status_var = tk.StringVar(value="Ready — drop or open a PDF")
        tk.Label(
            self,
            textvariable=self.status_var,
            anchor="w",
            font=(_FONT_FAMILY[0], 9),
        ).pack(padx=20, fill="x")

        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.pack(padx=20, pady=(2, 5), fill="x")

        self.output_var = tk.StringVar()
        self.output_entry = tk.Entry(
            self,
            textvariable=self.output_var,
            state="readonly",
            readonlybackground="white",
            relief="flat",
            font=(_FONT_FAMILY[0], 9),
        )
        self.output_entry.pack(padx=20, pady=(2, 15), fill="x")

    def _on_drag_enter(self, event):
        if not self._processing:
            self.drop_frame.configure(bg=self._drop_bg_hover)
            self.drop_label.configure(bg=self._drop_bg_hover)

    def _on_drag_leave(self, event):
        self.drop_frame.configure(bg=self._drop_bg_default)
        self.drop_label.configure(bg=self._drop_bg_default)

    def _on_drop(self, event):
        self.drop_frame.configure(bg=self._drop_bg_default)
        self.drop_label.configure(bg=self._drop_bg_default)

        if self._processing:
            self.status_var.set("Processing in progress — please wait")
            return

        paths = list(self.tk.splitlist(event.data))
        if len(paths) == 0:
            self.status_var.set("No file detected")
            return
        if len(paths) != 1:
            self.status_var.set("Drop one file at a time")
            return

        if not self._validate_pdf_extension(paths[0]):
            return
        self._split(paths[0])

    def _open_dialog(self):
        if self._processing:
            self.status_var.set("Processing in progress — please wait")
            return
        path = filedialog.askopenfilename(
            title="Select NorthBay Claim PDF",
            filetypes=[("PDF Files", "*.pdf"), ("All Files", "*.*")],
        )
        if path:
            if not self._validate_pdf_extension(path):
                return
            self._split(path)

    def _validate_pdf_extension(self, filepath: str) -> bool:
        _, ext = os.path.splitext(filepath)
        if ext.lower() != ".pdf":
            self.status_var.set(f"Unexpected file type: {ext}")
            messagebox.showwarning(
                "Unexpected File Type",
                f"File extension '{ext}' is not a PDF.\n\nThe file will not be processed.",
            )
            return False
        return True

    @staticmethod
    def _truncate_filename(name: str, max_len: int = 40) -> str:
        if len(name) <= max_len:
            return name
        return name[: max_len - 3] + "..."

    def _split(
        self,
        filepath: str,
        *,
        overwrite: bool = False,
        override_size_limit: bool = False,
    ):
        if self._processing or self._update_pending:
            return
        self._processing = True

        basename = self._truncate_filename(os.path.basename(filepath))
        self.status_var.set(f"Working on {basename}...")
        self.output_var.set("")
        self.progress.start(10)
        self.update_idletasks()

        def status(text: str):
            self._safe_after(0, self.status_var.set, text)

        def run():
            try:
                if self._client is None:
                    self._client = PbiClient()
                result = collate_pdf(
                    filepath,
                    client=self._client,
                    on_status=status,
                    overwrite=overwrite,
                    override_size_limit=override_size_limit,
                )
                self._safe_after(0, self._on_success, result)
            except CollationError as exc:
                self._safe_after(0, self._on_collation_error, exc)
            except EmptyPageError as exc:
                self._safe_after(0, self._on_empty_page, exc)
            except OutputExistsError as exc:
                self._safe_after(0, self._on_output_exists, filepath, exc)
            except InputTooLargeError as exc:
                self._safe_after(0, self._on_input_too_large, filepath, exc)
            except OutputCorruptError as exc:
                self._safe_after(0, self._on_output_corrupt, exc)
            except PbiError as exc:
                logger.error("Power BI step failed: %s", exc)
                self._safe_after(0, self._on_error, str(exc))
            except Exception as exc:
                logger.error("Split failed: %s", type(exc).__name__)
                self._safe_after(0, self._on_error, self._friendly_error(exc))

        threading.Thread(target=run, daemon=True).start()

    def _reset_processing(self):
        self._processing = False
        self.progress.stop()

    @staticmethod
    def _bullets(items: list[str], limit: int = 25) -> str:
        shown = "\n  - ".join(items[:limit])
        more = f"\n  ... and {len(items) - limit} more" if len(items) > limit else ""
        return f"  - {shown}{more}"

    def _on_success(self, result: CollateResult):
        self._reset_processing()
        parts = [
            f"{suffix} {invoice}: {result.invoice_pages[suffix]} invoice + {result.claim_pages[suffix]} claim pages"
            for suffix, invoice in result.invoices.items()
        ]
        self.status_var.set("Done. " + "; ".join(parts) + ".")
        if result.output_paths:
            self.output_var.set(os.path.dirname(result.output_paths[0]))
        else:
            self.output_var.set("")
        notes = []
        if result.unprinted_runs:
            notes.append(
                "These claims are not printed on their invoice (billed $0 or less) and were put at the end:\n"
                + self._bullets(result.unprinted_runs)
            )
        if result.claims_missing:
            notes.append(
                "These invoice lines have no 1500 in the dropped PDF:\n" + self._bullets(result.claims_missing)
            )
        if notes:
            messagebox.showwarning("Files Written - Please Review", "\n\n".join(notes))

    def _on_collation_error(self, exc: CollationError):
        self._reset_processing()
        self.status_var.set("Stopped — no files written")
        messagebox.showerror(
            "Claims and Invoices Don't Match",
            f"{exc}\n\n{self._bullets(exc.items)}\n\nNo output files were written.",
        )

    def _on_empty_page(self, exc: EmptyPageError):
        self._reset_processing()
        self.status_var.set("Image-only page detected — no files written")
        messagebox.showerror(
            "Empty Page",
            f"Page {exc.page_number} has no extractable text. "
            "The PDF may be image-only / scanned.\n\n"
            "No output files were written.",
        )

    def _on_output_exists(self, filepath: str, exc: OutputExistsError):
        self._reset_processing()
        names = "\n  - ".join(os.path.basename(p) for p in exc.existing_paths)
        proceed = messagebox.askyesno(
            "Outputs Already Exist",
            f"Outputs from a previous run exist:\n\n  - {names}\n\nOverwrite?",
        )
        if proceed:
            self._split(filepath, overwrite=True)
        else:
            self.status_var.set("Cancelled — no files written")

    def _on_input_too_large(self, filepath: str, exc: InputTooLargeError):
        self._reset_processing()
        size_mb = exc.size_bytes / (1024 * 1024)
        proceed = messagebox.askyesno(
            "Large Input PDF",
            f"This PDF has {exc.page_count} pages ({size_mb:.1f} MB) and may take several minutes. Continue?",
        )
        if proceed:
            self._split(filepath, override_size_limit=True)
        else:
            self.status_var.set("Cancelled — input too large")

    def _on_output_corrupt(self, exc: OutputCorruptError):
        self._reset_processing()
        self.status_var.set("Output verification failed — no files written")
        messagebox.showerror(
            "Output Verification Failed",
            f"Saved file failed page-count verification:\n\n"
            f"  {exc.output_path}\n\n"
            f"Expected {exc.expected_pages} pages, got {exc.actual_pages}.\n\n"
            "The bad temp file has been deleted. No final output files were written.",
        )

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        if isinstance(exc, FileNotFoundError):
            return "File not found. Please check the file path and try again."
        if isinstance(exc, PermissionError):
            return "Permission denied. Close the file if it is open in another program."
        if isinstance(exc, ValueError):
            return str(exc)
        logger.error("Unexpected error: %s", type(exc).__name__)
        return "An unexpected error occurred. Check the log for details."

    def _safe_after(self, ms, func, *args):
        try:
            self.after(ms, func, *args)
        except tk.TclError:
            self._processing = False
            with contextlib.suppress(tk.TclError):
                self.progress.stop()

    def _on_error(self, message: str):
        self._reset_processing()
        self.status_var.set("Error — see details")
        messagebox.showerror("Splitter Error", message)

    def _on_close(self):
        if self._processing:
            if messagebox.askokcancel(
                "Processing in Progress",
                "A split is still running.\n\nForce close the application?",
            ):
                self.destroy()
            return
        self.destroy()

    def _show_about(self):
        messagebox.showinfo(
            "About",
            f"NorthBay Claim Splitter v{__version__}\n\n"
            "Refreshes the NorthBay invoice data, sorts the CMS-1500\n"
            "pages by run number, and puts each invoice on top:\n\n"
            "  -NBC: NORTHBAY invoice (ALS/BLS/CCT/GURNEY/MCC/NEONATE)\n"
            "  -TX: NORTHBAY-WHA TRANSFER invoice\n"
            "  -911: NORTHBAY-WHA 911 invoice\n\n"
            "Each PDF gets a matching .xlsx claims summary.\n"
            "Output files land next to the input PDF.",
        )


if __name__ == "__main__":
    app = SplitterApp()
    app.mainloop()
