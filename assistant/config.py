import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env', override=False)
DATA_DIR = Path(os.environ.get('JARVIS_DATA_DIR', str(ROOT / 'data'))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / 'assistant.sqlite3'
STATIC_DIR = ROOT / 'assistant' / 'static'
