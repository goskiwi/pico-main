"""Run original benchmark tests in a separate evaluation checkout, outside model access."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def verify(config_path):
    config=json.loads(Path(config_path).read_text())
    source=Path(config['workspace']); evaluation=Path(config['evaluation'])
    for path in config['source_paths']:
        origin=source/path
        if origin.is_symlink() or not origin.is_file():
            raise ValueError('Source file missing or redirected: '+path)
        target=evaluation/path
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(origin,target)
    env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ['KEY','TOKEN','SECRET','PASSWORD'])}
    env.update(PYTHONPATH=str(evaluation/'src' if (evaluation/'src').is_dir() else evaluation),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
    started=time.monotonic()
    plugins=['-p','pytester'] if (evaluation/'src/_pytest').is_dir() else []
    result=subprocess.run([config['python'],'-m','pytest','-q','-o','addopts=',
                           '-p','no:cacheprovider',*plugins,*config['nodes']],cwd=evaluation,env=env,
                          check=False,text=True,capture_output=True,timeout=180)
    report={'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr,
            'seconds':time.monotonic()-started,'nodes':config['nodes']}
    Path(config['last_result']).write_text(json.dumps(report,indent=2))
    print(result.stdout,end=''); print(result.stderr,end='',file=sys.stderr)
    return result.returncode


if __name__=='__main__':
    raise SystemExit(verify(sys.argv[1]))
