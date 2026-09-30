"""Terminal output for setup: one line per step, with the running step's output nested under it.

On a terminal, the running step shows a spinner and its latest output, then collapses to one
line when it finishes; a step that fails keeps its output on screen. A step that needs input
stops collapsing and hands the terminal to the command, still nested. Anywhere else (a pipe,
CI) every line is printed as it arrives. Either way the full output goes to the log file."""
import atexit
import codecs
from contextlib import contextmanager
import os
import re
import selectors
import shlex
import shutil
import subprocess
import sys
import threading
import time

CONTROL = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)?|.)|[\x00-\x08\x0b-\x1f\x7f]")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
TAIL = 5            # output lines shown under a running step
FAILED_TAIL = 40    # output lines kept on screen when a step fails


def clean(text):
    return CONTROL.sub("", text.replace("\t", "    "))


def duration(seconds):
    seconds = int(seconds)
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


def fit(text, room):
    return text if len(text) <= room else text[:max(room - 1, 0)] + "…"


class Step:
    def __init__(self, title, number, total):
        self.title, self.number, self.total = title, number, total
        self.started = time.monotonic()
        self.lines = []
        self.note = None
        self.created = []
        self.reused = 0
        self.live = False

    def label(self):
        return (f"{self.number}/{self.total} " if self.number else "") + self.title


class Console:
    def __init__(self):
        self.tty = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
        self.color = self.tty and not os.environ.get("NO_COLOR")
        self.started = time.monotonic()
        self.current = None
        self.failed = None
        self.created_items = []
        self.log_path = None
        self._log = None
        self._pending_log = []
        self._lock = threading.RLock()
        self._drawn = 0
        self._ticker = None
        self._stop = threading.Event()
        self._partial = None    # a line a command is still writing, such as a prompt

    # Colors -------------------------------------------------------------------------------

    def _paint(self, code, text):
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def dim(self, text):
        return self._paint("2", text)

    def bold(self, text):
        return self._paint("1", text)

    def green(self, text):
        return self._paint("32", text)

    def red(self, text):
        return self._paint("31", text)

    def cyan(self, text):
        return self._paint("36", text)

    # Log ----------------------------------------------------------------------------------

    def open_log(self, path):
        """Only this user can read it: it holds everything the commands printed."""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        self._log = os.fdopen(fd, "w", encoding="utf-8")
        self.log_path = path
        for line in self._pending_log:
            self._log.write(line)
        self._pending_log = []
        self._log.flush()

    def _record(self, text):
        line = time.strftime("%H:%M:%S ") + text + "\n"
        if self._log:
            self._log.write(line)
            self._log.flush()
        else:
            self._pending_log.append(line)

    # Output -------------------------------------------------------------------------------

    def _write(self, text):
        sys.stdout.write(text)
        sys.stdout.flush()

    def _nested(self, text):
        return "    " + self.dim("│") + " " + text

    def say(self, text=""):
        """A line of its own, outside any step."""
        with self._lock:
            self._write(text + "\n")
            self._record(clean(text))

    def line(self, text):
        """A line of output that belongs to the running step."""
        text = clean(text)
        with self._lock:
            self._close_partial()
            self._record("  │ " + text)
            step = self.current
            if step is not None:
                step.lines.append(text)
            if step is None or not step.live:
                self._write(self._nested(text) + "\n")

    def note(self, text):
        """A short result shown on the step's line when it finishes."""
        if self.current:
            self.current.note = text

    def created(self, what):
        self.created_items.append(what)
        self.line("Created " + what)
        if self.current:
            self.current.created.append(what)

    def reused(self, what):
        self.line("Already there: " + what)
        if self.current:
            self.current.reused += 1

    def ask(self, prompt):
        with self._lock:
            self._write(self.bold(prompt))
        answer = input()
        self._record(prompt + answer)
        return answer

    # Steps --------------------------------------------------------------------------------

    @contextmanager
    def step(self, title, number=None, total=None):
        step = Step(title, number, total)
        with self._lock:
            self.current = step
            self._record("== " + step.label())
            if self.tty:
                step.live = True
            else:
                self._write("  " + self.cyan("›") + " " + step.label() + "\n")
        if step.live:
            self._start_ticker()
        try:
            yield step
        except BaseException:
            self._finish(step, ok=False)
            raise
        self._finish(step, ok=True)

    def _finish(self, step, ok):
        self._stop_ticker()
        with self._lock:
            self._close_partial()
            self._clear()
            parts = [step.note]
            if step.created:
                parts.append(f"{len(step.created)} created")
            elif step.reused:
                parts.append("already set up")
            parts.append(duration(time.monotonic() - step.started))
            suffix = self.dim(" · ".join(p for p in parts if p))
            icon = self.green("✓") if ok else self.red("✗")
            self._record(("== done: " if ok else "== failed: ") + step.label())
            if not ok and step.live:
                hidden = len(step.lines) - FAILED_TAIL
                self._write("  " + self.cyan("›") + " " + step.label() + "\n")
                if hidden > 0:
                    self._write(self._nested(self.dim(f"… {hidden} earlier lines are in the log")) + "\n")
                for text in step.lines[-FAILED_TAIL:]:
                    self._write(self._nested(text) + "\n")
                self._write("  " + self.red("✗") + " " + step.label() + "  " + suffix + "\n")
            else:
                self._write("  " + icon + " " + step.label() + "  " + suffix + "\n")
            step.live = False
            self.current = None
            if not ok:
                self.failed = step

    def _go_plain(self):
        """Stop collapsing the running step, so a command can talk to the person directly."""
        step = self.current
        if step is None or not step.live:
            return
        self._stop_ticker()
        with self._lock:
            self._clear()
            step.live = False
            self._write("  " + self.cyan("›") + " " + step.label() + "\n")
            for text in step.lines:
                self._write(self._nested(text) + "\n")

    # The live region: the running step's line and its latest output, redrawn in place ------

    def _start_ticker(self):
        if self._ticker is None:
            atexit.register(self._write, "\x1b[?25h")
        self._write("\x1b[?25l")
        self._stop.clear()
        self._ticker = threading.Thread(target=self._tick, daemon=True)
        self._ticker.start()

    def _stop_ticker(self):
        if self._ticker and self._ticker.is_alive():
            self._stop.set()
            self._ticker.join()
            self._write("\x1b[?25h")

    def _tick(self):
        while True:
            with self._lock:
                self._render()
            if self._stop.wait(0.1):
                return

    def _render(self):
        step = self.current
        if step is None or not step.live:
            return
        width = shutil.get_terminal_size().columns
        elapsed = duration(time.monotonic() - step.started)
        frame = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
        rows = ["  " + self.cyan(frame) + " "
                + fit(step.label(), width - len(elapsed) - 7) + "  " + self.dim(elapsed)]
        rows += [self._nested(self.dim(fit(text, width - 7))) for text in step.lines[-TAIL:]]
        self._clear()
        self._write("\n".join(rows) + "\n")
        self._drawn = len(rows)

    def _clear(self):
        if self._drawn:
            self._write(f"\x1b[{self._drawn}F\x1b[J")
            self._drawn = 0

    # Commands -----------------------------------------------------------------------------

    def run(self, args, *, cwd=None, secret=False, interactive=False):
        """Runs a command with its output nested in the running step. Returns (code, stdout, stderr)."""
        args = [str(x) for x in args]
        self.line("$ " + shlex.join(args))
        if interactive:
            return self._run_interactive(args, cwd)
        if secret:
            self.line("(output hidden: it is a credential)")
        proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = {"out": [], "err": []}
        pending = {"out": "", "err": ""}
        decoders = {name: codecs.getincrementaldecoder("utf-8")("replace") for name in output}
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, "out")
            selector.register(proc.stderr, selectors.EVENT_READ, "err")
            try:
                while selector.get_map():
                    for key, _ in selector.select():
                        name = key.data
                        data = os.read(key.fd, 65536)
                        text = decoders[name].decode(data, final=not data)
                        if not data:
                            selector.unregister(key.fileobj)
                        output[name].append(text)
                        if secret and name == "out":
                            continue
                        *lines, pending[name] = (pending[name] + text).split("\n")
                        if not data and pending[name]:
                            lines.append(pending[name])
                        for line in lines:
                            # Progress bars redraw with \r; keep what was drawn last.
                            self.line(line.rstrip("\r").rsplit("\r", 1)[-1])
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                proc.stdout.close()
                proc.stderr.close()
        return proc.returncode, "".join(output["out"]), "".join(output["err"])

    def _run_interactive(self, args, cwd):
        """Gives the command a terminal of its own, nested one level, and passes typing through."""
        self._go_plain()
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return subprocess.run(args, cwd=cwd).returncode, "", ""
        import fcntl
        import pty
        import struct
        import termios
        master, child_side = pty.openpty()
        size = shutil.get_terminal_size()
        fcntl.ioctl(child_side, termios.TIOCSWINSZ,
                    struct.pack("HHHH", size.lines, max(size.columns - 6, 20), 0, 0))
        # This terminal already echoes what the person types; don't echo it a second time.
        attrs = termios.tcgetattr(child_side)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(child_side, termios.TCSANOW, attrs)
        proc = subprocess.Popen(args, cwd=cwd, stdin=child_side, stdout=child_side,
                                stderr=child_side)
        os.close(child_side)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        output = []
        stdin = sys.stdin.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(master, selectors.EVENT_READ, "command")
            selector.register(stdin, selectors.EVENT_READ, "person")
            try:
                done = False
                while not done:
                    for key, _ in selector.select():
                        if key.data == "person":
                            typed = os.read(stdin, 4096)
                            if not typed:
                                selector.unregister(stdin)
                                typed = b"\x04"
                            os.write(master, typed)
                            if typed.endswith(b"\n"):
                                with self._lock:
                                    # The Enter the person typed already ended the line on screen.
                                    self._close_partial(newline=False)
                            continue
                        try:
                            data = os.read(master, 4096)
                        except OSError:
                            data = b""
                        if not data:
                            done = True
                            break
                        text = decoder.decode(data).replace("\r\n", "\n").replace("\r", "")
                        output.append(text)
                        self._stream(text)
            finally:
                os.close(master)
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
        with self._lock:
            self._close_partial()
        return proc.returncode, "".join(output), ""

    def _stream(self, text):
        """Writes output as it arrives, so a prompt without a newline shows before it's answered."""
        with self._lock:
            for piece in re.split(r"(\n)", text):
                if piece == "\n":
                    if self._partial is None:
                        self._write(self._nested("") + "\n")
                        self._keep("")
                    else:
                        self._close_partial()
                elif piece:
                    piece = clean(piece)
                    if self._partial is None:
                        self._write(self._nested(""))
                        self._partial = ""
                    self._write(piece)
                    self._partial += piece

    def _close_partial(self, newline=True):
        if self._partial is None:
            return
        if newline:
            self._write("\n")
        self._keep(self._partial)
        self._partial = None

    def _keep(self, text):
        self._record("  │ " + text)
        if self.current:
            self.current.lines.append(text)
