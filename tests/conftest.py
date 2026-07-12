"""Put the featuretree package root on sys.path so `import ir` / `import step_recognize` work when
pytest is run from anywhere."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
