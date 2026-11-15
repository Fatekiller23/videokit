"""python -m videokit 的入口。命令行本体在 __init__.main。"""

import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
