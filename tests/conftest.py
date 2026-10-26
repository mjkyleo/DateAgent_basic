"""pytest 配置：把仓库根加入 sys.path，使 `import basic.*` 可用。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
