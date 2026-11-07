import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# The tests never ask GitHub for a newer Delta (delta.updates); the update tests use a stand-in.
os.environ.setdefault("DELTA_NO_UPDATE_CHECK", "1")
