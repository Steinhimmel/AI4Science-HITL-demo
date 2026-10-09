"""Run a deterministic agent proposal + real human review, no model key needed."""
import argparse
import json
import time
from pathlib import Path
from ai4s.core import Lab


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',default='outputs/hitl_demo')
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True,exist_ok=True)
    lab = Lab(out/'lab.sqlite')
    try:
        print('SIMULATION ONLY. Startup requires operator reset.')
        if input('输入 RESET 确认复位（不会启动设备）: ').strip() != 'RESET':
            return
        lab.reset('operator')
        plan = lab.propose('agent',{'low':20,'high':70,'step':.1,'dwell':.01})
        print(json.dumps(plan,ensure_ascii=False,indent=2))
        if input('查看参数与摘要后，输入 APPROVE 批准；其他输入拒绝: ').strip() != 'APPROVE':
            lab.review('operator',plan['id'],plan['hash'],'REJECT','CLI human rejection')
            print('已拒绝，设备未启动。')
            return
        lab.review('operator',plan['id'],plan['hash'],'APPROVE','CLI human approval')
        lab.execute('agent',plan['id'])
        print('开始模拟扫描；Ctrl+C 可停止。')
        while lab.status()['state'] in ('RUNNING','STOPPING'):
            time.sleep(.1)
        snapshot = lab.status()
        (out/'xrd.csv').write_text(lab.export_csv(),encoding='utf-8-sig')
        (out/'xrd.json').write_text(json.dumps(snapshot['dataset'],ensure_ascii=False,indent=2),encoding='utf-8')
        print(snapshot['state']+'; data: '+str(out.resolve()))
    except KeyboardInterrupt:
        lab.stop('operator',emergency=True,reason='Human interrupted demo')
    finally:
        lab.close()


if __name__ == '__main__':
    main()
