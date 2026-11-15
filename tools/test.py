"""Run the SDK tests against this project source tree."""
import os
from pathlib import Path
import subprocess
import sys
root=Path(__file__).resolve().parents[1]
env={k:v for k,v in os.environ.items() if not k.startswith("CODERFORGE_")}
env["PYTHONPATH"]=str(root/"src")
subprocess.run([sys.executable,"-m","unittest","discover","-s","tests","-p","test_*.py"],cwd=root,env=env,check=True)
