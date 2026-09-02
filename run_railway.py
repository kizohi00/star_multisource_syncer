"""Railway entrypoint that works with the repository's src/ layout."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

print("[mangastar-syncer] entrypoint started", flush=True)

from mangastar_multisource.cli import main  # noqa: E402
from mangastar_multisource.config import load_local_env  # noqa: E402


LOCAL_ENV_FILE = PROJECT_ROOT / ".env"
if LOCAL_ENV_FILE.is_file():
    # Railway Variables already present in the environment win because
    # load_local_env() never overwrites an existing variable.
    load_local_env(LOCAL_ENV_FILE)


if __name__ == "__main__":
    main(sys.argv[1:] or ["worker", "--once", "--allow-source-failures"])
