"""Deterministic gate; SQLite records; simulation-only execution."""
import csv
import copy
import hashlib
import io
import json
import math
import queue
import sqlite3
import threading
import time
import uuid
from .devices import SimXRD


class Denied(ValueError):
    pass


CALL_SLOTS = threading.BoundedSemaphore(8)


def start_call(fn):
    if not CALL_SLOTS.acquire(blocking=False):
        raise TimeoutError('Device call capacity exhausted')
    result = queue.Queue(maxsize=1)
    def invoke():
        try:
            result.put((True, fn()))
        except Exception as exc:
            result.put((False, exc))
        finally:
            CALL_SLOTS.release()
    threading.Thread(target=invoke, daemon=True).start()
    return result


def finish_call(result, timeout):
    try:
        ok, value = result.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError('Device call timed out')
    if not ok:
        raise value
    return value


def bounded_call(fn, timeout=1):
    return finish_call(start_call(fn),timeout)


class Lab:
    def __init__(self, db=':memory:', device=None, monitor=True):
        self.device = device or SimXRD()
        if self.device.mode not in ('SIMULATION', 'OPHYD_SIMULATION'):
            raise Denied('Physical device execution is disabled in this prototype')
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(db), check_same_thread=False)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, time REAL, actor TEXT, action TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, payload TEXT);
        ''')
        old = self.db.execute("SELECT value FROM meta WHERE key='state'").fetchone()
        self.state = 'ESTOP' if old and old[0] == 'ESTOP' else 'RECOVERY'
        self.plans = {}
        self.epoch = 0
        self.cancel = threading.Event()
        self.cancel.set()
        self.worker = None
        self.stops_in_flight = 0
        self.io_quarantined = False
        self.dataset = None
        latest = self.db.execute('SELECT payload FROM datasets ORDER BY rowid DESC LIMIT 1').fetchone()
        if latest:
            self.dataset = json.loads(latest[0])
            if self.dataset.get('outcome') == 'RUNNING':
                self.dataset['outcome'] = 'INTERRUPTED'
                self.dataset['ended'] = time.time()
        self.feedback = None
        self.reason = 'Operator reset required after process startup'
        self.audit_fault = False
        self.alive = threading.Event()
        self.alive.set()
        self._record('system', 'boot', {'previous_state': old[0] if old else None})
        self._persist_state()
        self._save_dataset()
        if monitor:
            threading.Thread(target=self._monitor, daemon=True).start()

    def _record(self, actor, action, detail):
        try:
            self.db.execute('INSERT INTO events(time,actor,action,detail) VALUES(?,?,?,?)',
                            (time.time(), actor, action, json.dumps(detail, ensure_ascii=False, allow_nan=False)))
            self.db.commit()
        except Exception:
            self.audit_fault = True
            self.state = 'ESTOP'
            self.epoch += 1
            self.cancel.set()
            # Do not wait for the audit system before requesting safe outputs.
            threading.Thread(target=self.device.safe_stop, daemon=True).start()
            raise

    def _persist_state(self):
        try:
            self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('state',?)", (self.state,))
            self.db.commit()
        except Exception:
            self.audit_fault = True
            self.state = 'ESTOP'
            self.epoch += 1
            self.cancel.set()
            threading.Thread(target=self.device.safe_stop,daemon=True).start()
            raise

    def _permission(self, actor, allowed):
        if actor not in allowed:
            with self.lock:
                self._record(actor, 'denied', {'reason': 'Permission denied'})
            raise Denied('Permission denied')

    def _healthy(self, feedback, allow_output=False):
        return (isinstance(feedback, dict) and feedback.get('door_closed') is True
                and feedback.get('cooling_ok') is True
                and type(feedback.get('temperature_c')) in (int, float)
                and math.isfinite(feedback['temperature_c']) and feedback['temperature_c'] <= 45
                and (allow_output or feedback.get('output_on') is False))

    def status(self):
        with self.lock:
            events = self.db.execute('SELECT time,actor,action,detail FROM events ORDER BY id DESC LIMIT 40').fetchall()
            return dict(state=self.state, mode=self.device.mode, reason=self.reason,
                        feedback=self.feedback, epoch=self.epoch,
                        plans=copy.deepcopy(list(self.plans.values())),
                        dataset=copy.deepcopy(self.dataset), audit_fault=self.audit_fault,
                        io_quarantined=self.io_quarantined,
                        events=[dict(time=t, actor=a, action=c, detail=json.loads(d)) for t,a,c,d in events])

    @staticmethod
    def validate(params):
        if not isinstance(params, dict) or set(params) - {'low', 'high', 'step', 'dwell'}:
            raise Denied('Unsupported scan fields')
        values = {}
        for name, default in [('low',20), ('high',70), ('step',.1), ('dwell',.02)]:
            v = params.get(name, default)
            if type(v) not in (int, float) or not math.isfinite(v):
                raise Denied('Scan values must be finite numbers')
            values[name] = float(v)
        low, high, step, dwell = (values[k] for k in ('low','high','step','dwell'))
        if not (5 <= low < high <= 90 and .02 <= step <= 2 and .01 <= dwell <= 1):
            raise Denied('Limits: angles 5–90°, step .02–2°, dwell .01–1s')
        n = math.floor((high-low)/step + 1e-8) + 1
        if n > 5000 or n*dwell > 120:
            raise Denied('Scan exceeds point or exposure budget')
        return values

    def propose(self, actor, params):
        with self.lock:
            self._permission(actor, ('operator','agent'))
            try:
                values = self.validate(params)
            except Denied as exc:
                self._record(actor, 'plan_rejected', {'reason':str(exc)})
                raise
            if len(self.plans) >= 100:
                raise Denied('Plan quota reached; restart or expire plans')
            plan = dict(id=uuid.uuid4().hex, params=values, owner=actor, epoch=self.epoch,
                        hash=hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(),
                        approved=False, used=False, expires=time.time()+120,
                        review='PENDING', approved_by=None, review_comment='', device_mode=self.device.mode)
            self._record(actor,'propose',plan)
            self.plans[plan['id']] = plan
            return copy.deepcopy(plan)

    def review(self, actor, plan_id, expected_hash, decision, comment=''):
        """Trusted operator endpoint only. The agent cannot authorize its own proposal."""
        with self.lock:
            self._permission(actor, ('operator',))
            p = self.plans.get(plan_id)
            if not p or p['expires'] <= time.time() or p['epoch'] != self.epoch or p['used']:
                raise Denied('Plan invalid or expired')
            if expected_hash != p['hash'] or decision not in ('APPROVE','REJECT','REVOKE'):
                raise Denied('Review must match the displayed plan hash and decision')
            if not isinstance(comment,str) or len(comment) > 500:
                raise Denied('Review comment must be at most 500 characters')
            if decision == 'APPROVE' and p['review'] != 'PENDING':
                raise Denied('Rejected, revoked or already approved plans need a new proposal')
            self._record(actor,'human_review',{'id':plan_id,'hash':p['hash'],'decision':decision,'comment':comment})
            p['approved'] = decision == 'APPROVE'
            p['review'] = decision
            p['approved_by'] = actor if p['approved'] else None
            p['review_comment'] = comment
            if p['approved']:
                p['expires'] = min(p['expires'],time.time()+60)
            return copy.deepcopy(p)

    def approve(self, actor, plan_id, expected_hash):
        return self.review(actor,plan_id,expected_hash,'APPROVE')

    def execute(self, actor, plan_id):
        with self.lock:
            self._permission(actor, ('operator','agent'))
            checked_epoch = self.epoch
        # Read outside the safety lock so stop requests can preempt this check.
        feedback = bounded_call(self.device.status,.5)
        with self.lock:
            self._permission(actor, ('operator','agent'))
            p = self.plans.get(plan_id)
            if not p or not p['approved'] or p['used'] or p['expires'] <= time.time() or p['epoch'] != self.epoch:
                raise Denied('Requires unused, current, approved plan')
            if actor == 'agent' and p['owner'] != 'agent':
                raise Denied('Agent cannot execute operator plan')
            if self.state != 'IDLE' or self.audit_fault or self.io_quarantined or self.stops_in_flight or checked_epoch != self.epoch or (self.worker and self.worker.is_alive()):
                raise Denied('Device not ready; operator reset required')
            # No model-supplied parameters here: use the approved immutable plan.
            values = self.validate(p['params'])
            if hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest() != p['hash']:
                raise Denied('Plan changed after approval')
            if not self._healthy(feedback) or p['device_mode'] != self.device.mode:
                raise Denied('Interlocks or outputs not safe')
            self._record(actor,'execute',{'id':plan_id,'hash':p['hash']})
            p['used'] = True
            self.cancel = threading.Event()
            self.state = 'RUNNING'
            self.reason = ''
            self.dataset = dict(id=uuid.uuid4().hex, source=self.device.mode, units={'two_theta':'degree','intensity':'arbitrary_unit'},
                                started=time.time(), plan_id=plan_id, params=values, points=[], outcome='RUNNING')
            self._persist_state()
            generation, cancel = self.epoch, self.cancel
            self.worker = threading.Thread(target=self._scan, args=(generation,cancel,values), daemon=True)
            self.worker.start()
            return {'run_id':self.dataset['id'], 'state':self.state}

    def _save_dataset(self):
        if self.dataset:
            self.db.execute('INSERT OR REPLACE INTO datasets(id,payload) VALUES(?,?)',
                            (self.dataset['id'],json.dumps(self.dataset,allow_nan=False)))
            self.db.commit()

    def _scan(self, generation, cancel, params):
        try:
            with self.lock:
                if generation != self.epoch or cancel.is_set():
                    return
                self.device.arm()  # Only local simulation; physical arm is deliberately disabled.
            n = math.floor((params['high']-params['low'])/params['step'] + 1e-8)+1
            deadline = time.monotonic()+125
            for i in range(n):
                if cancel.is_set():
                    return
                angle = round(params['low']+i*params['step'],8)
                y = bounded_call(lambda: self.device.acquire(angle,params['dwell']), params['dwell']+.5)
                if type(y) not in (int,float) or not math.isfinite(y) or not 0 <= y <= 1e9:
                    raise RuntimeError('Invalid detector data')
                if time.monotonic() > deadline:
                    raise TimeoutError('Workflow deadline exceeded')
                with self.lock:
                    if generation != self.epoch or cancel.is_set():
                        return
                    self.dataset['points'].append({'two_theta':angle,'intensity':float(y),'time':time.time()})
                    if i % 20 == 0:
                        self._save_dataset()
            self.stop('system', completed=True)
        except Exception as exc:
            if not cancel.is_set():
                self.stop('system', emergency=True, reason=str(exc))

    def stop(self, actor, emergency=False, completed=False, reason='Stop requested'):
        stop_result = None
        start_error = None
        with self.lock:
            self._permission(actor, ('operator','agent','system'))
            self.epoch += 1
            generation = self.epoch
            self.cancel.set()
            self.stops_in_flight += 1
            latched = emergency or self.state == 'ESTOP'
            self.state = 'ESTOP' if latched else 'STOPPING'
            self.reason = reason
            self.plans.clear()
            # Dispatch output shutdown before potentially slow database/audit writes.
            try:
                stop_result = start_call(self.device.safe_stop)
            except Exception as exc:
                start_error = exc
            if self.dataset and self.dataset['outcome'] == 'RUNNING':
                self.dataset['outcome'] = 'ESTOP' if latched else ('COMPLETED' if completed else 'STOPPED')
                self.dataset['ended'] = time.time()
            try:
                self._record(actor,'estop' if latched else 'stop',{'reason':reason})
                self._persist_state()
                self._save_dataset()
            except Exception:
                latched = True
        try:
            if start_error:
                raise start_error
            finish_call(stop_result,.5)
            feedback = bounded_call(self.device.status, .5)
            if feedback.get('output_on') is not False:
                raise RuntimeError('Safe output not confirmed')
            with self.lock:
                self.feedback = dict(feedback, observed=time.time())
                if generation == self.epoch and self.state != 'ESTOP':
                    self.state = 'COMPLETED' if completed else 'STOPPED'
                self._persist_state()
        except Exception as exc:
            with self.lock:
                self.state = 'ESTOP'
                self.reason = 'Stop unconfirmed: '+str(exc)
                if isinstance(exc,TimeoutError):
                    self.io_quarantined = True
                if self.dataset and self.dataset.get('outcome') in ('COMPLETED','STOPPED'):
                    self.dataset['outcome'] = 'ESTOP'
                try:
                    self._persist_state()
                    self._save_dataset()
                except Exception:
                    self.audit_fault = True
        finally:
            with self.lock:
                self.stops_in_flight -= 1
        return {'state':self.state, 'reason':self.reason}

    def reset(self, actor):
        with self.lock:
            self._permission(actor, ('operator',))
            checked_epoch = self.epoch
            if self.state in ('RUNNING','STOPPING') or self.stops_in_flight or self.io_quarantined or (self.worker and self.worker.is_alive()) or self.audit_fault:
                raise Denied('Reset blocked: active worker or audit fault')
        # Read outside safety lock. Reset never arms the device.
        feedback = bounded_call(self.device.status,.5)
        with self.lock:
            if checked_epoch != self.epoch or self.stops_in_flight or self.state in ('RUNNING','STOPPING') or not self._healthy(feedback):
                raise Denied('Reset blocked: unhealthy interlocks or output on')
            self.epoch += 1
            self.plans.clear()
            self._record(actor,'reset',{})
            self.state = 'IDLE'
            self.reason = ''
            self.feedback = dict(feedback, observed=time.time())
            self._persist_state()
            return {'state':self.state}

    def inject(self, actor, name, value):
        with self.lock:
            self._permission(actor, ('operator',))
            self.device.inject(name,value)
            self._record(actor,'simulation_fault',{'name':name,'value':value})
        try:
            if not self._healthy(self.device.status(),allow_output=True):
                return self.stop('system',emergency=True,reason='Simulation interlock fault')
        except ConnectionError:
            return self.stop('system',emergency=True,reason='Simulation connection lost')
        return {'state':self.state}

    def _monitor(self):
        while self.alive.is_set():
            try:
                feedback = bounded_call(self.device.status,.5)
                with self.lock:
                    self.feedback = dict(feedback,observed=time.time())
                    unsafe = not self._healthy(feedback,allow_output=self.state=='RUNNING')
                    trip = unsafe and self.state != 'ESTOP'
                if trip:
                    self.stop('system',emergency=True,reason='Monitor interlock or unexpected output')
            except Exception:
                with self.lock:
                    trip = self.state != 'ESTOP'
                if trip:
                    self.stop('system',emergency=True,reason='Device status unavailable')
            time.sleep(.2)

    def import_data(self, actor, payload, fmt='csv'):
        self._permission(actor, ('operator',))
        if not isinstance(payload,str) or len(payload.encode()) > 1000000:
            raise Denied('Upload too large or invalid')
        if fmt == 'csv':
            reader = csv.DictReader(io.StringIO(payload.lstrip('\ufeff')))
            if reader.fieldnames != ['two_theta','intensity']:
                raise Denied('CSV header must be two_theta,intensity')
            rows = list(reader)
        elif fmt == 'json':
            data = json.loads(payload)
            if not isinstance(data,dict) or not isinstance(data.get('points'),list):
                raise Denied('JSON must contain points array')
            rows = data['points']
        else:
            raise Denied('Unsupported format')
        if not 1 <= len(rows) <= 10000:
            raise Denied('Dataset must contain 1–10000 points')
        points = []
        for r in rows:
            if not isinstance(r,dict) or isinstance(r.get('two_theta'),bool) or isinstance(r.get('intensity'),bool):
                raise Denied('Invalid point')
            x,y = float(r['two_theta']),float(r['intensity'])
            if not math.isfinite(x) or not math.isfinite(y) or not 0 <= x <= 180 or not 0 <= y <= 1e9:
                raise Denied('Invalid angle or intensity')
            if points and x <= points[-1]['two_theta']:
                raise Denied('Angles must strictly increase')
            points.append(dict(two_theta=x,intensity=y))
        with self.lock:
            if self.state in ('RUNNING','STOPPING'):
                raise Denied('Import blocked while experiment active')
            self.dataset = dict(id=uuid.uuid4().hex,source='IMPORTED_UNVERIFIED',
                                units={'two_theta':'degree','intensity':'arbitrary_unit'},
                                started=time.time(),points=points,outcome='IMPORTED')
            self._record(actor,'import',{'id':self.dataset['id'],'count':len(points)})
            self._save_dataset()
            return {'dataset_id':self.dataset['id'],'count':len(points)}

    def export_csv(self):
        with self.lock:
            out = io.StringIO()
            writer = csv.DictWriter(out,fieldnames=['two_theta','intensity'],extrasaction='ignore')
            writer.writeheader()
            writer.writerows(self.dataset['points'] if self.dataset else [])
            return out.getvalue()

    def close(self):
        self.alive.clear()
        self.stop('system',reason='Service shutdown')
