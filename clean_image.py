#!/usr/bin/env python
import os
import sys
from build_config import *

if not os.geteuid()==0:
    sys.exit('This script must be run as root!')

os.system(f"zfs destroy -r {ZFS_CWD}/.install")