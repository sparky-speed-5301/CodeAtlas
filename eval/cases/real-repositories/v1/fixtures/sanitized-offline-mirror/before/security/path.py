from pathlib import Path

def load(root: Path, name: str) -> str:
    return (root / name).read_text()
