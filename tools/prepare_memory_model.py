"""Explicitly prepare pinned noncommercial Jina memory assets; never run at startup."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config.memory_jina import (JINA_DEFAULT_PATH, JINA_FILES, JINA_LICENSE,
    JINA_MEMORY_VERSION, JINA_MODEL_ID, JINA_REVISION, verify_jina_snapshot)


def prepare(*, destination=JINA_DEFAULT_PATH, source=None, download=False):
    """Publish one complete verified directory, or leave no destination behind."""
    if (source is None) == (not download):
        raise ValueError("choose_exactly_one_of_source_or_download")
    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        return verify_jina_snapshot(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".jina-prepare-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "snapshot"
        staged.mkdir()
        for name, (size, _digest) in JINA_FILES.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source is not None:
                shutil.copyfile(Path(source).expanduser() / name, target)
            else:
                url = f"https://huggingface.co/{JINA_MODEL_ID}/resolve/{JINA_REVISION}/{name}"
                with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as output:
                    count = 0
                    while block := response.read(min(1024 * 1024, size + 1 - count)):
                        count += len(block)
                        if count > size:
                            raise ValueError("model_corrupt")
                        output.write(block)
        verify_jina_snapshot(staged)
        (staged / "NOTICE.md").write_text(
            f"# Jina memory embedding model\n\n{JINA_MODEL_ID}\nRevision: {JINA_REVISION}\n\n"
            "Attribution: Jina AI GmbH. Noncommercial model weights and tokenizer.\n"
            f"License: {JINA_LICENSE} https://creativecommons.org/licenses/by-nc/4.0/\n"
            f"Source: https://huggingface.co/{JINA_MODEL_ID}/tree/{JINA_REVISION}\n"
            "Pinned upstream README.md is included. No model source code is executed.\n", encoding="utf-8")
        (staged / "prepared.json").write_text(json.dumps({"model": JINA_MODEL_ID,
            "revision": JINA_REVISION, "version": JINA_MEMORY_VERSION, "license": JINA_LICENSE,
            "files": JINA_FILES}, indent=2) + "\n", encoding="utf-8")
        # A concurrent publisher must also have the exact pinned bytes. No
        # partially copied files become visible at the configured destination.
        try:
            os.rename(staged, destination)
        except FileExistsError:
            verify_jina_snapshot(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--source", type=Path, help="Existing pinned snapshot, copied and verified offline")
    mode.add_argument("--download", action="store_true", help="Explicit network preparation of pinned assets")
    parser.add_argument("--destination", type=Path, default=JINA_DEFAULT_PATH)
    args = parser.parse_args()
    print(prepare(destination=args.destination, source=args.source, download=args.download))


if __name__ == "__main__":
    main()
