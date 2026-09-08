"""Listing the GenomeArk bucket.

Anonymous ``ListObjectsV2`` over plain HTTPS, which is why boto3 and the
AWS CLI are not dependencies: the REST response carries keys, sizes and
ETags, and the bucket needs no credentials.

**Listing is always delimited.** ``callable_sites/`` holds zarr stores —
3,264 objects for a single species — so a recursive listing of an
accession is thousands of requests' worth of response for data no check
reads. :func:`AccessionInventory` descends only into ``bams/``,
``vcfs/`` and ``qc/``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urlencode

from congen.core import http
from congen.core.cache import Cache

BUCKET = "genomeark"
BASE_URL = f"https://{BUCKET}.s3.amazonaws.com"
VARIANT_CALLING_PREFIX = "downstream_analyses/conservation_genomics/variant_calling"

S3_NS = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}

#: Prefixes never worth walking: zarr stores and their indexes.
OPAQUE_SUBDIRS = frozenset({"callable_sites"})

#: Subdirectories an accession is expected to publish.
EXPECTED_SUBDIRS = ("bams", "vcfs", "qc", "callable_sites")

LISTING_TTL = 3600.0


@dataclass(frozen=True)
class S3Object:
    key: str
    size: int
    etag: str
    last_modified: str

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.key}"

    @property
    def is_empty(self) -> bool:
        return self.size == 0


@dataclass(frozen=True)
class Listing:
    prefix: str
    #: Immediate subdirectory names, without trailing slash.
    subdirs: tuple[str, ...] = ()
    objects: tuple[S3Object, ...] = ()

    def by_name(self) -> dict[str, S3Object]:
        return {obj.name: obj for obj in self.objects}

    def names(self) -> set[str]:
        return {obj.name for obj in self.objects}


def _parse_listing(prefix: str, documents: list[ET.Element]) -> Listing:
    subdirs: list[str] = []
    objects: list[S3Object] = []
    for root in documents:
        for element in root.findall("s:CommonPrefixes/s:Prefix", S3_NS):
            text = (element.text or "").rstrip("/")
            if text:
                subdirs.append(text.rsplit("/", 1)[-1])
        for element in root.findall("s:Contents", S3_NS):
            key = element.findtext("s:Key", default="", namespaces=S3_NS)
            if not key or key.endswith("/"):
                continue  # directory marker
            objects.append(
                S3Object(
                    key=key,
                    size=int(element.findtext("s:Size", default="0", namespaces=S3_NS) or 0),
                    etag=(
                        element.findtext("s:ETag", default="", namespaces=S3_NS) or ""
                    ).strip('"'),
                    last_modified=element.findtext(
                        "s:LastModified", default="", namespaces=S3_NS
                    )
                    or "",
                )
            )
    return Listing(prefix=prefix, subdirs=tuple(sorted(subdirs)), objects=tuple(objects))


@dataclass
class AccessionInventory:
    """What one accession publishes, without touching the zarr stores."""

    accession: str
    prefix: str
    exists: bool = False
    top: Listing | None = None
    bams: Listing | None = None
    vcfs: Listing | None = None
    qc: Listing | None = None

    @property
    def subdirs(self) -> tuple[str, ...]:
        return self.top.subdirs if self.top else ()

    @property
    def bam_objects(self) -> dict[str, S3Object]:
        """Sample name -> BAM object."""
        if not self.bams:
            return {}
        return {o.name[:-4]: o for o in self.bams.objects if o.name.endswith(".bam")}

    @property
    def bam_index_names(self) -> set[str]:
        if not self.bams:
            return set()
        return {o.name for o in self.bams.objects if o.name.endswith((".csi", ".bai"))}

    @property
    def vcf_names(self) -> set[str]:
        if not self.vcfs:
            return set()
        return {o.name for o in self.vcfs.objects if o.name.endswith(".vcf.gz")}

    def object(self, *path: str) -> S3Object | None:
        """Look up an object by its path relative to the accession."""
        listings = {
            (): self.top,
            ("bams",): self.bams,
            ("vcfs",): self.vcfs,
            ("qc",): self.qc,
        }
        listing = listings.get(tuple(path[:-1]))
        return listing.by_name().get(path[-1]) if listing else None

    @property
    def empty_objects(self) -> list[S3Object]:
        out: list[S3Object] = []
        for listing in (self.top, self.bams, self.vcfs, self.qc):
            if listing:
                out.extend(o for o in listing.objects if o.is_empty)
        return out


class GenomeArk:
    """Read-only access to the conservation-genomics prefix."""

    def __init__(
        self,
        cache: Cache | None = None,
        *,
        base_prefix: str = VARIANT_CALLING_PREFIX,
        listing_ttl: float = LISTING_TTL,
    ) -> None:
        self.cache = cache or Cache()
        self.base_prefix = base_prefix.rstrip("/")
        self.listing_ttl = listing_ttl

    def url(self, key: str) -> str:
        return f"{BASE_URL}/{key}"

    def accession_prefix(self, accession: str) -> str:
        return f"{self.base_prefix}/{accession}"

    def list(self, prefix: str, *, delimiter: str = "/") -> Listing:
        """List one prefix, following continuation tokens.

        ``delimiter=""`` would recurse; nothing in this codebase passes
        it, and ``callable_sites/`` is why.
        """
        cache_key = f"{prefix}|{delimiter}"
        cached = self.cache.get("s3-listing", cache_key, ttl=self.listing_ttl)
        if cached is not None:
            return Listing(
                prefix=prefix,
                subdirs=tuple(cached["subdirs"]),
                objects=tuple(S3Object(**o) for o in cached["objects"]),
            )

        documents: list[ET.Element] = []
        token: str | None = None
        while True:
            params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if delimiter:
                params["delimiter"] = delimiter
            if token:
                params["continuation-token"] = token
            body = http.get_bytes(f"{BASE_URL}/?{urlencode(params)}")
            root = ET.fromstring(body)
            documents.append(root)
            truncated = (
                root.findtext("s:IsTruncated", default="false", namespaces=S3_NS) or "false"
            )
            if truncated.lower() != "true":
                break
            token = root.findtext("s:NextContinuationToken", namespaces=S3_NS)
            if not token:
                break

        listing = _parse_listing(prefix, documents)
        self.cache.set(
            "s3-listing",
            cache_key,
            {
                "subdirs": list(listing.subdirs),
                "objects": [vars(o) for o in listing.objects],
            },
        )
        return listing

    def accessions(self) -> list[str]:
        """Every accession published under the variant-calling prefix."""
        listing = self.list(f"{self.base_prefix}/")
        return [name for name in listing.subdirs if name.startswith("GC")]

    def inventory(self, accession: str) -> AccessionInventory:
        """Inventory one accession, skipping opaque subdirectories."""
        prefix = self.accession_prefix(accession)
        top = self.list(f"{prefix}/")
        inventory = AccessionInventory(accession=accession, prefix=prefix)
        if not top.subdirs and not top.objects:
            return inventory

        inventory.exists = True
        inventory.top = top
        for name in top.subdirs:
            if name in OPAQUE_SUBDIRS:
                continue
            listing = self.list(f"{prefix}/{name}/")
            if name == "bams":
                inventory.bams = listing
            elif name == "vcfs":
                inventory.vcfs = listing
            elif name == "qc":
                inventory.qc = listing
        return inventory
