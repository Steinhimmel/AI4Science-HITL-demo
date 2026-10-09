import copy
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
from ai4s.core import Lab, Denied
from ai4s.devices import SimXRD
from ai4s.server import handler_for


class CountingDevice(SimXRD):
    def __init__(self):
        super().__init__()
        self.arms = 0

    def arm(self):
        self.arms += 1
        super().arm()


class HumanReviewTests(unittest.TestCase):
    def setUp(self):
        self.device = CountingDevice()
        self.lab = Lab(device=self.device,monitor=False)
        self.lab.reset('operator')

    def tearDown(self):
        self.lab.close()
        if self.lab.worker:
            self.lab.worker.join(1)

    def plan(self):
        return self.lab.propose('agent',{'low':20,'high':21,'step':.1,'dwell':.01})

    def approve(self,p):
        self.lab.approve('operator',p['id'],p['hash'])

    def test_no_side_effect_before_human_approval(self):
        p = self.plan()
        with self.assertRaises(Denied):
            self.lab.execute('agent',p['id'])
        self.assertEqual(self.device.arms,0)
        self.assertFalse(self.device.output_on)
        self.approve(p)
        self.lab.execute('agent',p['id'])
        self.lab.worker.join(2)
        self.assertEqual(self.device.arms,1)
        self.assertEqual(self.lab.state,'COMPLETED')
        self.assertEqual(len(self.lab.dataset['points']),11)
        self.assertFalse(self.device.output_on)

    def test_agent_cannot_approve_reset_or_inject(self):
        p = self.plan()
        for fn in (lambda:self.lab.approve('agent',p['id'],p['hash']),
                   lambda:self.lab.reset('agent'),
                   lambda:self.lab.inject('agent','door_closed',True)):
            with self.assertRaises(Denied):
                fn()
        self.assertFalse(self.lab.plans[p['id']]['approved'])

    def test_review_matches_displayed_hash(self):
        p = self.plan()
        with self.assertRaises(Denied):
            self.lab.approve('operator',p['id'],'wrong displayed hash')
        self.assertEqual(self.device.arms,0)

    def test_rejection_revocation_and_expiry(self):
        for decision in ('REJECT','REVOKE'):
            p = self.plan()
            if decision=='REVOKE':
                self.approve(p)
            self.lab.review('operator',p['id'],p['hash'],decision,'Human decision')
            with self.assertRaises(Denied):
                self.lab.execute('agent',p['id'])
            with self.assertRaises(Denied):
                self.approve(p)
        p = self.plan()
        self.approve(p)
        self.lab.plans[p['id']]['expires']=time.time()-1
        with self.assertRaises(Denied):
            self.lab.execute('agent',p['id'])
        self.assertEqual(self.device.arms,0)

    def test_plan_copy_and_tamper_detection(self):
        p = self.plan()
        p['params']['high']=89
        self.assertEqual(self.lab.plans[p['id']]['params']['high'],21)
        p = self.plan()
        self.approve(p)
        snapshot = self.lab.status()
        snapshot['plans'][1]['params']['high']=88
        self.assertEqual(self.lab.plans[p['id']]['params']['high'],21)
        # Tamper inside the trusted host: hash verification still blocks changed parameters.
        self.lab.plans[p['id']]['params']['high']=22
        with self.assertRaises(Denied):
            self.lab.execute('agent',p['id'])

    def test_replay_and_stop_invalidate_approvals(self):
        p = self.plan()
        self.approve(p)
        self.lab.execute('agent',p['id'])
        with self.assertRaises(Denied):
            self.lab.execute('agent',p['id'])
        self.lab.stop('agent')
        self.lab.worker.join(1)
        self.lab.reset('operator')
        with self.assertRaises(Denied):
            self.lab.execute('agent',p['id'])
        q = self.plan()
        self.approve(q)
        self.lab.stop('operator',emergency=True)
        self.lab.stop('agent')
        self.assertEqual(self.lab.state,'ESTOP')
        self.lab.reset('operator')
        self.assertEqual(self.device.arms,1)
        with self.assertRaises(Denied):
            self.lab.execute('agent',q['id'])

    def test_live_interlock_and_stop_feedback(self):
        p = self.plan()
        self.approve(p)
        self.lab.inject('operator','door_closed',False)
        with self.assertRaises(Denied):
            self.lab.execute('agent',p['id'])
        with self.assertRaises(Denied):
            self.lab.reset('operator')
        self.lab.inject('operator','door_closed',True)
        self.assertEqual(self.lab.state,'ESTOP')
        self.lab.reset('operator')
        p = self.plan()
        self.approve(p)
        self.lab.execute('agent',p['id'])
        time.sleep(.02)
        self.device.stop_failure=True
        self.lab.stop('operator')
        self.assertEqual(self.lab.state,'ESTOP')
        with self.assertRaises(Denied):
            self.lab.reset('operator')
        self.device.stop_failure=False

    def test_timeout_latches_and_no_late_data(self):
        self.device.delay = 1
        p = self.plan()
        self.approve(p)
        self.lab.execute('agent',p['id'])
        self.lab.worker.join(2)
        self.assertEqual(self.lab.state,'ESTOP')
        n=len(self.lab.dataset['points'])
        time.sleep(.03)
        self.assertEqual(n,len(self.lab.dataset['points']))

    def test_reset_rechecks_epoch_after_feedback(self):
        entered,release = threading.Event(),threading.Event()
        original = self.device.status
        def delayed():
            feedback = original()
            if threading.current_thread().name != 'MainThread':
                entered.set()
                release.wait(.3)
            return feedback
        self.device.status = delayed
        result=[]
        def reset():
            try:
                self.lab.reset('operator')
                result.append('RESET')
            except Denied:
                result.append('DENIED')
        thread=threading.Thread(target=reset)
        thread.start()
        self.assertTrue(entered.wait(1))
        # Simulate an emergency arriving during the external state read.
        with self.lab.lock:
            self.lab.epoch += 1
            self.lab.state='ESTOP'
        release.set()
        thread.join(1)
        self.assertEqual(result,['DENIED'])
        self.assertEqual(self.lab.state,'ESTOP')
        self.device.status=original

    def test_bounds_and_untrusted_import(self):
        for params in ({'step':0},{'low':float('nan')},{'dwell':True},{'disable_interlock':True}):
            with self.assertRaises(Denied):
                self.lab.propose('agent',params)
        self.lab.import_data('operator','two_theta,intensity\n20,1\n21,2\n')
        self.assertEqual(self.lab.dataset['source'],'IMPORTED_UNVERIFIED')
        self.assertEqual(self.device.arms,0)
        with self.assertRaises(Denied):
            self.lab.import_data('agent','two_theta,intensity\n20,1\n')
        with self.assertRaises(Denied):
            self.lab.import_data('operator','two_theta,intensity\n20,nan\n')

    def test_audit_failure_blocks_execution(self):
        p=self.plan()
        self.approve(p)
        self.lab.db.execute("CREATE TRIGGER audit_failure BEFORE INSERT ON events BEGIN SELECT RAISE(FAIL,'disk failure'); END")
        with self.assertRaises(Exception):
            self.lab.execute('agent',p['id'])
        self.assertEqual(self.device.arms,0)
        self.assertTrue(self.lab.audit_fault)
        self.assertEqual(self.lab.state,'ESTOP')
        self.lab.db.execute('DROP TRIGGER audit_failure')


class RestartTests(unittest.TestCase):
    def test_estop_survives_restart_and_plans_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'lab.sqlite'
            first=Lab(db,monitor=False)
            first.reset('operator')
            p=first.propose('agent',{})
            first.approve('operator',p['id'],p['hash'])
            first.stop('operator',emergency=True)
            first.db.close()
            second=Lab(db,monitor=False)
            self.assertEqual(second.state,'ESTOP')
            self.assertEqual(second.plans,{})
            second.reset('operator')
            self.assertEqual(second.state,'IDLE')
            second.db.close()


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.lab=Lab(monitor=False)
        self.lab.reset('operator')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.lab,{'operator':'human-test-token','agent':'agent-test-token'}))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.lab.close()
        if self.lab.worker:
            self.lab.worker.join(1)

    def request(self,path,data=None,token='agent-test-token'):
        req=urllib.request.Request('http://127.0.0.1:%s/api/'%self.server.server_port+path,
            data=None if data is None else json.dumps(data).encode(),
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=2) as response:
                return response.status,response.read()
        except urllib.error.HTTPError as exc:
            return exc.code,exc.read()

    def test_agent_identity_cannot_self_approve(self):
        status,raw=self.request('propose',{'params':{'low':20,'high':21,'step':.1,'dwell':.01}})
        self.assertEqual(status,200)
        p=json.loads(raw)
        review={'plan_id':p['id'],'expected_hash':p['hash'],'decision':'APPROVE'}
        self.assertEqual(self.request('review',review)[0],403)
        self.assertEqual(self.request('review',dict(review,actor='operator'))[0],403)
        self.assertEqual(self.request('reset',{})[0],403)
        self.assertEqual(self.request('execute',{'plan_id':p['id']})[0],403)
        self.assertEqual(self.request('review',review,'human-test-token')[0],200)
        self.assertEqual(self.request('execute',{'plan_id':p['id']})[0],200)
        self.lab.worker.join(2)
        self.assertEqual(self.request('export.csv')[0],200)
        self.assertEqual(self.request('status',token='wrong')[0],403)


class InteractiveDemoTests(unittest.TestCase):
    def test_scripted_human_approval_demo(self):
        # Simulated human input for testing only; the real CLI waits for input.
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([sys.executable,'demo_hitl.py','--output',directory],
                input='RESET\nAPPROVE\n',stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                encoding='utf-8',errors='replace',timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            data=json.loads((Path(directory)/'xrd.json').read_text(encoding='utf-8'))
            self.assertEqual(data['outcome'],'COMPLETED')
            self.assertEqual(len(data['points']),501)
            self.assertEqual(data['source'],'SIMULATION')

    def test_rejected_demo_has_no_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([sys.executable,'demo_hitl.py','--output',directory],
                input='RESET\nREJECT\n',stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                encoding='utf-8',errors='replace',timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertFalse((Path(directory)/'xrd.json').exists())


if __name__=='__main__':
    unittest.main()
