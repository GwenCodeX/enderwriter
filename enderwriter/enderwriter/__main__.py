"""让 ``python -m enderwriter`` 等价于 ``python -m enderwriter.cli``。

用法::

    python -m enderwriter demo --window 8000 --llm stub
    python -m enderwriter stats --db ./demo.db
    python -m enderwriter query -e 沈砚,断魂崖 -q "月蚀之夜" --budget 800
    python -m enderwriter patrol --chapter ch_003
    python -m enderwriter export-schemas --out ./schemas
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
