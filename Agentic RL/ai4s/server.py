"""Local demo gateway: identities are taken from credentials, never request fields."""
import argparse
import json
import secrets
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .core import Lab, Denied
from .devices import OphydSimXRD

ROOT = Path(__file__).resolve().parent.parent


def handler_for(lab, tokens):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, data, code=200, mime='application/json'):
            raw = (json.dumps(data,ensure_ascii=False,allow_nan=False) if mime=='application/json' else data).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type',mime+'; charset=utf-8')
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(raw)

        def identity(self):
            credential = self.headers.get('Authorization','')[7:]
            if not self.headers.get('Authorization','').startswith('Bearer '):
                raise Denied('Bearer credential required')
            for role,token in tokens.items():
                if secrets.compare_digest(credential,token):
                    return role
            raise Denied('Invalid credential')

        def do_GET(self):
            try:
                if self.path == '/':
                    return self.send((ROOT/'dashboard.html').read_text(encoding='utf-8'),mime='text/html')
                self.identity()
                if self.path == '/api/status':
                    return self.send(lab.status())
                if self.path == '/api/export.csv':
                    return self.send(lab.export_csv(),mime='text/csv')
                if self.path == '/api/export.json':
                    return self.send(lab.status()['dataset'] or {'points':[]})
                self.send({'error':'Not found'},404)
            except Denied as exc:
                self.send({'error':str(exc)},403)

        def do_POST(self):
            try:
                role = self.identity()
                origin = self.headers.get('Origin')
                if origin and origin not in ('http://127.0.0.1:%s'%self.server.server_port,'http://localhost:%s'%self.server.server_port):
                    raise Denied('Cross-origin mutation denied')
                length = int(self.headers.get('Content-Length',0))
                if not 0 < length <= 1100000:
                    raise ValueError('Body too large or empty')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data,dict):
                    raise ValueError('JSON object required')
                # Do not permit role/identity injection by clients or model arguments.
                fields = {
                    '/api/propose': {'params'}, '/api/review':{'plan_id','expected_hash','decision','comment'},
                    '/api/execute': {'plan_id'}, '/api/stop': set(), '/api/estop': set(),
                    '/api/reset': set(), '/api/fault':{'name','value'}, '/api/import':{'payload','format'},
                }
                if self.path not in fields:
                    return self.send({'error':'Not found'},404)
                if set(data)-fields[self.path]:
                    raise Denied('Unsupported fields; identity cannot be supplied in JSON')
                if self.path == '/api/propose':
                    result = lab.propose(role,data['params'])
                elif self.path == '/api/review':
                    result = lab.review(role,data['plan_id'],data['expected_hash'],data['decision'],data.get('comment',''))
                elif self.path == '/api/execute':
                    result = lab.execute(role,data['plan_id'])
                elif self.path == '/api/stop':
                    result = lab.stop(role)
                elif self.path == '/api/estop':
                    result = lab.stop(role,emergency=True,reason='Emergency stop requested')
                elif self.path == '/api/reset':
                    result = lab.reset(role)
                elif self.path == '/api/fault':
                    result = lab.inject(role,data['name'],data['value'])
                else:
                    result = lab.import_data(role,data['payload'],data.get('format','csv'))
                self.send(result)
            except Denied as exc:
                self.send({'error':str(exc)},403)
            except (ValueError,KeyError,TypeError) as exc:
                self.send({'error':str(exc)},400)
            except Exception:
                self.send({'error':'Execution or storage fault; inspect status'},500)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8080)
    parser.add_argument('--ophyd-sim',action='store_true')
    args = parser.parse_args()
    runtime = ROOT/'.runtime'
    runtime.mkdir(exist_ok=True)
    # Rotate credentials on every restart; never pass operator credentials to the model.
    tokens = {role:secrets.token_urlsafe(32) for role in ('operator','agent')}
    for role,token in tokens.items():
        (runtime/(role+'.token')).write_text(token,encoding='utf-8')
    lab = Lab(runtime/'lab.sqlite',device=OphydSimXRD() if args.ophyd_sim else None)
    server = ThreadingHTTPServer(('127.0.0.1',args.port),handler_for(lab,tokens))
    print('AI4S simulation: http://127.0.0.1:%s'%args.port)
    print('Operator credential file: '+str(runtime/'operator.token'))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        lab.close()
        server.server_close()


if __name__ == '__main__':
    main()
