from pathlib import Path

def load(root: Path, name: str) -> str:
    return Path(name).read_text()
