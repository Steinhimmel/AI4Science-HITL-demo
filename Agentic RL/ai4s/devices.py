"""Only simulated hardware is enabled. No vendor commands are guessed."""
import math
import threading
from typing import Protocol


class Device(Protocol):
    mode: str
    def arm(self): ...
    def acquire(self, angle: float, dwell: float) -> float: ...
    def status(self) -> dict: ...
    def safe_stop(self): ...


def profile(angle):
    return 18.0 + sum(a * math.exp(-((angle-c)/w)**2) for c, a, w in
                      [(28.44, 940, .24), (47.30, 530, .30), (56.12, 315, .36)])


class SimXRD:
    mode = 'SIMULATION'

    def __init__(self):
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.stopped.set()
        self.door_closed = True
        self.cooling_ok = True
        self.connected = True
        self.temperature_c = 24.0
        self.output_on = False
        self.delay = 0.0
        self.stop_failure = False

    def status(self):
        with self.lock:
            if not self.connected:
                raise ConnectionError('Device disconnected')
            return dict(door_closed=self.door_closed, cooling_ok=self.cooling_ok,
                        temperature_c=self.temperature_c, output_on=self.output_on)

    def arm(self):
        with self.lock:
            if not self.connected or not self.door_closed or not self.cooling_ok:
                raise RuntimeError('Hardware simulation interlock')
            # Each run has its own cancellation latch; never re-enable an old read.
            self.stopped = threading.Event()
            self.output_on = True

    def acquire(self, angle, dwell):
        with self.lock:
            cancellation = self.stopped
        if cancellation.wait(dwell + self.delay):
            raise RuntimeError('Acquisition cancelled')
        with self.lock:
            if cancellation.is_set() or cancellation is not self.stopped or not self.output_on:
                raise RuntimeError('Device is stopped')
            return profile(angle)

    def safe_stop(self):
        with self.lock:
            if self.stop_failure:
                raise RuntimeError('Stop feedback failure')
            self.stopped.set()
            self.output_on = False

    def inject(self, name, value):
        types = {'door_closed': bool, 'cooling_ok': bool, 'connected': bool,
                 'temperature_c': (int, float), 'delay': (int, float), 'stop_failure': bool}
        if name not in types or not isinstance(value, types[name]):
            raise ValueError('Invalid fault setting')
        if name in ('temperature_c', 'delay') and (isinstance(value, bool) or not math.isfinite(value)):
            raise ValueError('Finite number required')
        if name == 'delay' and not 0 <= value <= 5:
            raise ValueError('Delay must be 0–5 seconds')
        with self.lock:
            setattr(self, name, value)
            # Simulation of an independent physical interlock.
            if not self.door_closed or not self.cooling_ok or self.temperature_c > 45:
                self.stopped.set()
                self.output_on = False


class OphydSimXRD(SimXRD):
    """Actual open-source Ophyd API calls, still simulated axes/detector."""
    mode = 'OPHYD_SIMULATION'

    def __init__(self):
        super().__init__()
        from ophyd.sim import SynAxis, SynSignal
        self.axis = SynAxis(name='two_theta')
        self.detector = SynSignal(name='intensity', func=lambda: profile(self.axis.position))

    def acquire(self, angle, dwell):
        with self.lock:
            cancellation = self.stopped
        if cancellation.wait(dwell + self.delay):
            raise RuntimeError('Acquisition cancelled')
        with self.lock:
            if cancellation.is_set() or cancellation is not self.stopped:
                raise RuntimeError('Device stopped')
            # SynAxis/SynSignal are local simulations, not blocking physical I/O.
            self.axis.set(angle).wait(timeout=.5)
            self.detector.trigger().wait(timeout=.5)
            return float(self.detector.read()['intensity']['value'])

    def safe_stop(self):
        super().safe_stop()
        self.axis.stop()
