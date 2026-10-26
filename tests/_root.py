"""测试的公共入口：把仓库根目录放进 sys.path，并给出 ROOT。

测试文件放在 tests/ 下，但被测的程序文件必须留在仓库根目录（已安装机器的
升级器按归档根目录取文件）。每个测试在导入被测模块前先 `import _root`。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
