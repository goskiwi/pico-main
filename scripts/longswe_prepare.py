"""Prepare three fixed official 128K rows and isolated local acceptance environments."""
import argparse
import json
import subprocess
import zipfile
from pathlib import Path

import pyarrow as pa

IDS = ['pytest-dev__pytest-10051', 'sympy__sympy-24213', 'pallets__flask-5014']


def command(args, *, cwd=None, env=None):
    result = subprocess.run(args,cwd=cwd,env=env,check=False,capture_output=True,text=True,timeout=300)
    if result.returncode:
        raise RuntimeError(f'{args[:3]}: {result.stdout[-1500:]} {result.stderr[-1500:]}')
    return result.stdout


def prepare(root):
    with zipfile.ZipFile(root/'LongSWE_Bench.zip') as archive:
        rows = pa.ipc.open_stream(archive.read('LongSWE_Bench/128K/test/data-00000-of-00001.arrow')).read_all().to_pylist()
    metadata=[]
    for instance in IDS:
        row = max((r for r in rows if r['instance_id']==instance),key=lambda r:r['num_tokens'])
        directory=root/instance
        directory.mkdir(exist_ok=True)
        (directory/'instance.json').write_text(json.dumps(row,ensure_ascii=False))
        (directory/'test.patch').write_text(row['test_patch'])
        base=directory/'base'
        if not (base/'.git').exists():
            base.mkdir()
            command(['git','init','-q'],cwd=base)
            command(['git','remote','add','origin',f'https://github.com/{row["repo"]}.git'],cwd=base)
            command(['git','fetch','--depth','1','origin',row['base_commit']],cwd=base)
            command(['git','checkout','--detach','FETCH_HEAD'],cwd=base)
        actual=command(['git','rev-parse','HEAD'],cwd=base).strip()
        if actual!=row['base_commit']:
            raise RuntimeError('wrong benchmark base commit')
        metadata.append({k:row[k] for k in ['instance_id','repo','base_commit','num_files','num_tokens']})
        print(json.dumps(metadata[-1]),flush=True)
    (root/'selection.json').write_text(json.dumps(metadata,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args()
    prepare(args.root.resolve())
