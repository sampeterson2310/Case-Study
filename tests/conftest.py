import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "charity-donor-outreach"
sys.path.insert(0, str(SKILL / "scripts"))
