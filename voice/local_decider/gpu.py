"""Total GPU memory in use, for the VRAM figures a run records.

Reported for the whole card, since per-process figures read N/A under WDDM (measured).
"""

import subprocess


def gpu_used_mib():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    return int(out.split()[0])
