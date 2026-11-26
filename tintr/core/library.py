"""Hero library of one TVPaint project: character references added once, labelled once, picked per layer.

Layout next to the manifest:
    <project_dir>/<project>_tintr/library/
        library.json
        <hero>.png          copies of the reference files (moving the originals breaks nothing)
        labels_<id>.png     the reference with its parts labelled by the VLM
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from tintr.naming import TAG_RE

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")


def match_key(name: str) -> str:
    """Name form used to match a layer to a hero: no tag, case, spaces or dashes. 'Ricky 1' == 'ricky-1_'."""
    base = TAG_RE.split(name)[0]
    return re.sub(r"[\s\-_]+", "_", base).strip("_").lower()


def safe_stem(stem: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_.") or "hero"


@dataclass
class Hero:
    id: str
    name: str
    file: str  # relative to the library dir
    # the parts labelled by the VLM (as LayerEntry.ref_labels), the drawn image (relative to the
    # manifest root) and the reference mtime it was made from
    labels: list = field(default_factory=list)
    labeled_file: str = ""
    labels_mtime: float = 0.0
    cost: float = 0.0
    error: str = ""


class Library:
    def __init__(self, root: Path):
        """root: the manifest root (<project>_tintr)."""
        self.root = Path(root)
        self.dir = self.root / "library"
        self.file = self.dir / "library.json"
        self.heroes: dict[str, Hero] = {}
        self._lock = threading.RLock()

    # ---------- io ----------
    @classmethod
    def load(cls, root: Path) -> "Library":
        lib = cls(root)
        if lib.file.exists():
            names = {f.name for f in fields(Hero)}
            data = json.loads(lib.file.read_text(encoding="utf-8"))
            for h in data.get("heroes", []):
                hero = Hero(**{k: v for k, v in h.items() if k in names})
                lib.heroes[hero.id] = hero
        return lib

    def save(self) -> None:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            data = {"heroes": [asdict(h) for h in self.heroes.values()]}
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.file)

    # ---------- paths / state ----------
    def path(self, h: Hero) -> Path:
        return self.dir / h.file

    def labeled_path(self, h: Hero) -> Path | None:
        """The labelled reference, if it was made from the current hero file."""
        if not h.labeled_file:
            return None
        p, src = self.root / h.labeled_file, self.path(h)
        if not (p.exists() and src.exists()) or abs(src.stat().st_mtime - h.labels_mtime) > 1e-3:
            return None
        return p

    def is_labeled(self, h: Hero) -> bool:
        return self.labeled_path(h) is not None

    def sorted(self) -> list[Hero]:
        return sorted(self.heroes.values(), key=lambda h: h.name.lower())

    def match(self, layer_name: str) -> Hero | None:
        key = match_key(layer_name)
        return next((h for h in self.heroes.values() if key and match_key(h.name) == key), None)

    # ---------- edits ----------
    def _free_file(self, stem: str, ext: str, keep: Hero | None = None) -> str:
        used = {h.file.lower() for h in self.heroes.values() if h is not keep}
        name, n = f"{stem}{ext}", 2
        while name.lower() in used:
            name, n = f"{stem}_{n}{ext}", n + 1
        return name

    def add(self, name: str, src: Path | None = None, data: bytes | None = None) -> tuple[Hero, bool]:
        """Add a reference (a file to copy, or its bytes). A hero with the same name gets the new picture
        and loses its labels. Returns (hero, replaced)."""
        stem, ext = Path(name).stem, Path(name).suffix.lower()
        if ext not in IMAGE_EXT:
            raise ValueError(f"{name}: референс має бути PNG, JPG або WEBP.")
        with self._lock:
            same = next((h for h in self.heroes.values() if h.name.lower() == stem.lower()), None)
            hero = same or Hero(id=uuid.uuid4().hex[:8], name=stem, file="")
            self.dir.mkdir(parents=True, exist_ok=True)
            old = self.path(hero) if hero.file else None
            hero.file = self._free_file(safe_stem(stem), ext, keep=hero)
            if old and old.exists() and old.name.lower() != hero.file.lower():
                old.unlink(missing_ok=True)
            dst = self.path(hero)
            if src is not None:
                if Path(src).resolve() != dst.resolve():
                    shutil.copyfile(src, dst)
            else:
                dst.write_bytes(data or b"")
            self.clear_labels(hero)
            self.heroes[hero.id] = hero
            self.save()
            return hero, same is not None

    def replace_file(self, hero: Hero, name: str, src: Path | None = None, data: bytes | None = None) -> None:
        ext = Path(name).suffix.lower()
        if ext not in IMAGE_EXT:
            raise ValueError(f"{name}: референс має бути PNG, JPG або WEBP.")
        with self._lock:
            old = self.path(hero)
            hero.file = self._free_file(safe_stem(Path(hero.file).stem), ext, keep=hero)
            if old.exists() and old.name.lower() != hero.file.lower():
                old.unlink(missing_ok=True)
            if src is not None:
                shutil.copyfile(src, self.path(hero))
            else:
                self.path(hero).write_bytes(data or b"")
            self.clear_labels(hero)
            self.save()

    def rename(self, hero: Hero, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("Імʼя героя не може бути порожнім.")
        if any(h is not hero and h.name.lower() == name.lower() for h in self.heroes.values()):
            raise ValueError(f"Герой «{name}» уже є в бібліотеці.")
        hero.name = name
        self.save()

    def remove(self, hero: Hero) -> None:
        with self._lock:
            self.path(hero).unlink(missing_ok=True)
            if hero.labeled_file:
                (self.root / hero.labeled_file).unlink(missing_ok=True)
            self.heroes.pop(hero.id, None)
            self.save()

    def clear_labels(self, hero: Hero) -> None:
        if hero.labeled_file:
            (self.root / hero.labeled_file).unlink(missing_ok=True)
        hero.labels, hero.labeled_file, hero.labels_mtime, hero.error = [], "", 0.0, ""
