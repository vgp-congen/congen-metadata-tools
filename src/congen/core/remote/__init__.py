"""Access to remote data: GenomeArk, NCBI, ENA."""

from congen.core.remote.headers import (
    BamHeader,
    BytesByteSource,
    FileByteSource,
    HeaderError,
    HttpByteSource,
    VcfHeader,
    bgzf_inflate,
    read_bam_header,
    read_vcf_header,
)

__all__ = [
    "BamHeader",
    "BytesByteSource",
    "FileByteSource",
    "HeaderError",
    "HttpByteSource",
    "VcfHeader",
    "bgzf_inflate",
    "read_bam_header",
    "read_vcf_header",
]
