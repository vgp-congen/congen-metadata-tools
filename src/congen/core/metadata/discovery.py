"""Finding and enumerating species in a congen-metadata checkout."""

from __future__ import annotations

import os
from functools import cached_property
from pathlib import Path
from typing import Iterator

from congen.core.metadata.loaders import load_config, load_readme, load_sample_sheet
from congen.core.metadata.models import SpeciesMetadata
from congen.core.metadata.vgp import (
    DEFAULT_RELATIVE_PATH,
    VgpReferenceList,
    load_vgp_list,
)

ENV_ROOT = "CONGEN_METADATA_ROOT"
SPECIES_DIRNAME = "species"
CONFIG_NAME = "config.yaml"
SHEET_NAME = "sample_sheet.csv"
README_NAME = "README.txt"


class MetadataRootNotFound(RuntimeError):
    pass


def _looks_like_root(path: Path) -> bool:
    return (path / SPECIES_DIRNAME).is_dir()


def find_metadata_root(start: Path | None = None) -> Path:
    """Locate a congen-metadata checkout.

    Order: ``$CONGEN_METADATA_ROOT``, then the starting directory and each
    ancestor, checking the directory itself and any ``congen-metadata``
    sibling. The sibling case is the normal local layout, where both repos
    are cloned next to each other.
    """
    env = os.environ.get(ENV_ROOT)
    if env:
        candidate = Path(env).expanduser()
        if not _looks_like_root(candidate):
            raise MetadataRootNotFound(
                f"{ENV_ROOT}={env} does not contain a {SPECIES_DIRNAME}/ directory"
            )
        return candidate.resolve()

    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        if _looks_like_root(directory):
            return directory
        sibling = directory / "congen-metadata"
        if _looks_like_root(sibling):
            return sibling.resolve()
    raise MetadataRootNotFound(
        f"no congen-metadata checkout found from {here}; "
        f"pass --metadata-root or set {ENV_ROOT}"
    )


class SpeciesRepo:
    """A congen-metadata checkout."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    @classmethod
    def discover(cls, start: Path | None = None) -> SpeciesRepo:
        return cls(find_metadata_root(start))

    def __repr__(self) -> str:
        return f"SpeciesRepo({str(self.root)!r})"

    @property
    def species_root(self) -> Path:
        return self.root / SPECIES_DIRNAME

    @cached_property
    def clades(self) -> list[str]:
        if not self.species_root.is_dir():
            return []
        return sorted(p.name for p in self.species_root.iterdir() if p.is_dir())

    def species_dirs(self, clade: str | None = None) -> list[Path]:
        """Every species directory, sorted by ``clade/slug``."""
        clades = [clade] if clade else self.clades
        out: list[Path] = []
        for name in clades:
            directory = self.species_root / name
            if not directory.is_dir():
                continue
            out.extend(
                p for p in sorted(directory.iterdir()) if p.is_dir() and (p / CONFIG_NAME).exists()
            )
        return out

    def resolve(self, target: str | Path) -> Path:
        """Resolve a CLI-style target to a species directory.

        Accepts ``clade/slug``, ``species/clade/slug``, a bare slug when it
        is unambiguous, or a filesystem path.
        """
        raw = str(target).strip().rstrip("/")

        candidate = Path(raw)
        if candidate.is_dir() and (candidate / CONFIG_NAME).exists():
            return candidate.resolve()

        parts = [p for p in raw.replace(os.sep, "/").split("/") if p and p != "."]
        if parts and parts[0] == SPECIES_DIRNAME:
            parts = parts[1:]
        if len(parts) >= 2:
            direct = self.species_root.joinpath(*parts[-2:])
            if (direct / CONFIG_NAME).exists():
                return direct
        if len(parts) == 1:
            matches = [p for p in self.species_dirs() if p.name == parts[0]]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                keys = ", ".join(f"{p.parent.name}/{p.name}" for p in matches)
                raise ValueError(f"{parts[0]!r} is ambiguous: {keys}")
        raise ValueError(f"no species directory matches {target!r} under {self.species_root}")

    def load_dir(self, directory: Path) -> SpeciesMetadata:
        directory = Path(directory)
        return SpeciesMetadata(
            slug=directory.name,
            clade=directory.parent.name,
            path=directory,
            config=load_config(directory / CONFIG_NAME),
            sheet=load_sample_sheet(directory / SHEET_NAME),
            readme=load_readme(directory / README_NAME),
        )

    def load(self, target: str | Path) -> SpeciesMetadata:
        return self.load_dir(self.resolve(target))

    def iter_species(self, clade: str | None = None) -> Iterator[SpeciesMetadata]:
        for directory in self.species_dirs(clade):
            yield self.load_dir(directory)

    @cached_property
    def vgp_list(self) -> VgpReferenceList:
        return load_vgp_list(self.root / DEFAULT_RELATIVE_PATH)
