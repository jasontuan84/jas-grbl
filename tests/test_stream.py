"""Tests for the GRBL character-counting streamer (serial_io).

No real hardware: a GRBL-like fake serial port simulates the 128-byte RX ring and an
asynchronous 'ok'-per-line executor, so streaming exercises real buffer accounting,
stop-on-error, user abort, pause/resume, and resume-from-line reconstruction.

Run directly:  python tests/test_stream.py
Or with pytest: pytest tests/
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jasgrbl_pkg import serial_io as S  # noqa: E402


class FakeGrbl:
    """GRBL 1.1 RX-ring + asynchronous ok-per-line simulator.

    Completed lines are not acked instantly; a background executor pops them at a limited
    rate (simulating motion time), so the host must pipeline ahead and genuinely fills the
    RX buffer - exactly the condition that tests the accounting. It ASSERTS the ring never
    exceeds RX_BUFFER_SIZE (128); a byte-counting bug in the streamer fails loudly here."""

    def __init__(self, exec_delay=0.0008):
        self.is_open = True
        self._cur = bytearray()
        self._out = bytearray()
        self._resident = 0            # bytes received, not yet acked
        self._queue = []              # byte-lengths of complete lines awaiting ack
        self._lock = threading.Lock()
        self.max_resident = 0
        self.lines_acked = 0
        self.state = "Run"
        self._exec_delay = exec_delay
        self._stop = False
        self._exec = threading.Thread(target=self._run_exec, daemon=True)
        self._exec.start()

    def _run_exec(self):
        while not self._stop:
            with self._lock:
                if self._queue and self.state != "Hold":   # motion/acking stops on Hold
                    self._ack_one()
            time.sleep(self._exec_delay)

    def _ack_one(self):
        ln = self._queue.pop(0)
        self._resident -= ln
        self._out += b"ok\n"
        self.lines_acked += 1

    def write(self, data):
        with self._lock:
            for b in bytes(data):
                if b == ord('?'):
                    self._out += ("<%s|WPos:1.000,2.000,0.000|FS:0,0>\n"
                                  % self.state).encode()
                    continue
                if b in (0x18, ord('!'), ord('~'), 0x9e, 0x85):   # realtime: not buffered
                    if b == 0x18:
                        self._resident = 0
                        self._queue.clear()
                        self._out += b"Grbl 1.1h ['$' for help]\n"
                    elif b == ord('!'):
                        self.state = "Hold"
                    elif b == ord('~'):
                        self.state = "Run"
                    continue
                self._cur.append(b)
                self._resident += 1
                self.max_resident = max(self.max_resident, self._resident)
                assert self._resident <= S.RX_BUFFER_SIZE, (
                    "RX OVERFLOW: %d bytes resident (>%d)"
                    % (self._resident, S.RX_BUFFER_SIZE))
                if b == ord('\n'):
                    self._queue.append(len(self._cur))
                    self._cur = bytearray()
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        with self._lock:
            return len(self._out)

    def read(self, n=1):
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            with self._lock:
                if self._out:
                    out = bytes(self._out[:n])
                    del self._out[:n]
                    return out
            time.sleep(0.001)
        return b""

    def readline(self):
        return self.read(256)

    def reset_input_buffer(self):
        with self._lock:
            self._out.clear()

    def close(self):
        self._stop = True
        self.is_open = False


class FakeGrblError(FakeGrbl):
    """Acks error:20 on the Nth line, to exercise stop-on-error."""

    def __init__(self, error_at, **kw):
        self._error_at = error_at
        super().__init__(**kw)

    def _ack_one(self):
        ln = self._queue.pop(0)
        self._resident -= ln
        self.lines_acked += 1
        self._out += (b"error:20\n" if self.lines_acked == self._error_at else b"ok\n")


def _manager(fake):
    logs = []
    mgr = S.SerialManager(lambda a, m: logs.append((a, m)))
    mgr._ser = fake
    return mgr, logs


# --------------------------------------------------------------- program prep
def test_prepare_program_strips():
    raw = ["G21\r", "; a comment", "(paren comment)", "  ", "G1 X1 Y1", ""]
    assert S.SerialManager._prepare_program(raw) == ["G21", "G1 X1 Y1"]


# --------------------------------------------------------------- streaming
def test_stream_invariant_and_completeness():
    fake = FakeGrbl()
    mgr, logs = _manager(fake)
    program = ["G21", "G90", "M4 S800"] + \
        ["G1 X%.3f Y%.3f F1200" % (i * 0.01, i * 0.02) for i in range(2000)]
    done, seen = {}, []
    mgr.stream(program, on_progress=lambda s, t: seen.append((s, t)),
               on_done=lambda ok: done.setdefault("ok", ok))
    mgr._stream_thread.join(timeout=30)
    assert not mgr._stream_thread.is_alive()
    assert done.get("ok") is True, logs[-5:]
    total = len(program)
    assert mgr.last_acked_index == total
    assert fake.lines_acked == total
    assert seen[-1] == (total, total)
    assert fake.max_resident <= S.RX_BUFFER_SIZE            # never overflowed the ring
    assert fake.max_resident > 30                           # but did keep it pipelined


def test_stop_on_error():
    fake = FakeGrblError(error_at=50)
    mgr, logs = _manager(fake)
    done = {}
    mgr.stream(["G1 X%d Y%d" % (i, i) for i in range(500)],
               on_done=lambda ok: done.setdefault("ok", ok))
    mgr._stream_thread.join(timeout=10)
    assert done.get("ok") is False
    assert 40 <= mgr.last_acked_index <= 60
    assert any(a == "ERROR" for a, _ in logs)


def test_user_abort():
    fake = FakeGrbl(exec_delay=0.004)
    mgr, _ = _manager(fake)
    done = {}
    mgr.stream(["G1 X%d Y%d" % (i, i) for i in range(5000)],
               on_done=lambda ok: done.setdefault("ok", ok))
    time.sleep(0.3)
    assert mgr.is_streaming()
    mgr.abort_stream()
    mgr._stream_thread.join(timeout=10)
    assert done.get("ok") is False
    assert 0 < mgr.last_acked_index < 5000


def test_pause_resume_in_place():
    fake = FakeGrbl(exec_delay=0.003)
    mgr, _ = _manager(fake)
    done = {}
    mgr.stream(["G1 X%d Y%d" % (i, i) for i in range(4000)],
               on_done=lambda ok: done.setdefault("ok", ok))
    time.sleep(0.2)
    mgr.feed_hold()
    time.sleep(0.2)
    assert mgr.is_paused()
    at_pause = mgr.last_acked_index
    time.sleep(0.3)
    assert mgr.last_acked_index - at_pause <= 8             # progress halted while paused
    mgr.resume()
    mgr._stream_thread.join(timeout=15)
    assert done.get("ok") is True                           # completes cleanly after resume
    assert mgr.last_acked_index == 4000


# --------------------------------------------------------------- resume-from-line
def test_resume_preamble_absolute():
    prog = ["G21", "G90", "M4 S900", "G1 X10 Y20 F1500", "G1 X30 Y40",
            "G1 X50 Y60", "G1 X70 Y80"]
    pre, reason = S.build_resume_preamble(prog, 5)
    assert pre is not None, reason
    assert "G21" in pre and "G90" in pre and "M5 S0" in pre
    assert any(p.startswith("G0 X30 Y40") for p in pre)     # rapid to last pos before N
    assert any(p.startswith("M4 S900") for p in pre)        # re-arm laser after travel
    off = [i for i, p in enumerate(pre) if p.startswith("G0 X")][0]
    assert pre.index("M5 S0") < off                         # beam off BEFORE travel


def test_resume_refuses_relative():
    prog = ["G21", "G91", "G1 X10 Y10 F1000", "G1 X10 Y10", "G1 X10 Y10"]
    pre, reason = S.build_resume_preamble(prog, 3)
    assert pre is None and "relative" in reason.lower()


def test_resume_refuses_mid_arc():
    prog = ["G21", "G90", "G1 X0 Y0 F1000", "G2 X10 Y10 I5 J0", "G1 X20 Y20"]
    pre, reason = S.build_resume_preamble(prog, 3)
    assert pre is None and "arc" in reason.lower()


def _run_all():
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in funcs:
        try:
            fn()
            passed += 1
            print("PASS  %s" % fn.__name__)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL  %s: %s" % (fn.__name__, exc))
    print("\n%d passed, %d failed" % (passed, failed))
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
