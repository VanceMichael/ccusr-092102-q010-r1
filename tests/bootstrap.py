"""让 `python3 -m unittest discover -s tests` 在未安装包时也能找到 src/ 布局。"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
