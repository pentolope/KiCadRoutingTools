"""python3 ballast_run.py N script.py [args...] -- run script.py with N live
objects allocated first, which shifts every id() it goes on to observe."""
import os
import runpy
import sys

n = int(sys.argv[1])
script = sys.argv[2]
BALLAST = [[0] * 7 for _ in range(n)]     # kept alive on purpose
sys.argv = [script] + sys.argv[3:]
root = os.path.dirname(os.path.dirname(os.path.abspath(script)))
sys.path.insert(0, os.path.join(root, 'py_router'))
sys.path.insert(0, root)
# run_path, unlike `python script.py`, does not add the script's own directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
runpy.run_path(script, run_name='__main__')
