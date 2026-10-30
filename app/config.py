"""Project-local configuration; explicit process environment takes precedence."""

from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def load_environment(root: Path = ROOT) -> None:
    # Never search the caller's working directory or expand ${...} inside keys.
    load_dotenv(root / ".env", override=False, interpolate=False, encoding="utf-8-sig")
