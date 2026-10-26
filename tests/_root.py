"""测试的公共入口：把程序目录 app/ 放进 sys.path，并给出路径常量。

被测的程序文件在仓库的 app/ 下，测试在 tests/ 下。每个测试在导入被测模块前
先 `import _root`。ROOT 指向程序目录（测试里读 bot.py 等都用它），REPO 是仓库根。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / 'app'
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
