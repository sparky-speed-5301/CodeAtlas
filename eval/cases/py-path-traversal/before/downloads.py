from pathlib import Path


def download(root: Path, requested: str) -> bytes:
    return (root / requested).read_bytes()
