"""Tests for the range-read header reader."""

from __future__ import annotations

import struct

import pytest

from congen.core.remote.headers import (
    BytesByteSource,
    FileByteSource,
    HeaderError,
    bgzf_inflate,
    parse_bam_header_bytes,
    parse_vcf_header_lines,
    read_bam_header,
    read_vcf_header,
)
from tests.conftest import FIXTURE_WINDOW, RecordingSource, bgzf_pack

MINIMAL_VCF = (
    "##fileformat=VCFv4.2\n"
    "##contig=<ID=chr1,length=1000>\n"
    "##contig=<ID=chr2,length=500>\n"
    '##GATKCommandLine=<ID=HaplotypeCaller,CommandLine="HaplotypeCaller '
    '--sample-ploidy 2 --heterozygosity 0.005 --output x.vcf">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE_A\tSAMPLE_B\n"
    "chr1\t1\t.\tA\tT\t50\tPASS\t.\tGT\t0/1\t1/1\n"
)


class TestBgzfInflate:
    def test_multi_member_stream(self):
        payload = b"x" * 5000
        assert bgzf_inflate(bgzf_pack(payload, block_size=512)) == payload

    def test_truncated_final_member_is_discarded(self):
        packed = bgzf_pack(b"A" * 512 + b"B" * 512, block_size=512)
        # Cut inside the second member: the first must still come back.
        cut = len(bgzf_pack(b"A" * 512, block_size=512)) + 10
        assert bgzf_inflate(packed[:cut]) == b"A" * 512

    def test_garbage_returns_empty_rather_than_raising(self):
        assert bgzf_inflate(b"not gzip at all") == b""

    def test_empty_input(self):
        assert bgzf_inflate(b"") == b""


class TestVcfHeaderParsing:
    def test_contigs_and_samples(self):
        header = parse_vcf_header_lines(MINIMAL_VCF.split("\n"))
        assert header.contigs == {"chr1": 1000, "chr2": 500}
        assert header.samples == ["SAMPLE_A", "SAMPLE_B"]

    def test_contig_without_length(self):
        header = parse_vcf_header_lines(["##contig=<ID=chrX>", "#CHROM\tPOS"])
        assert header.contigs == {"chrX": None}

    def test_gatk_argument_extraction(self):
        header = parse_vcf_header_lines(MINIMAL_VCF.split("\n"))
        assert header.gatk_argument("sample-ploidy") == "2"
        assert header.gatk_argument("heterozygosity") == "0.005"
        assert header.gatk_argument("not-a-flag") is None

    def test_stops_at_chrom_line(self):
        header = read_vcf_header(BytesByteSource(bgzf_pack(MINIMAL_VCF.encode())))
        assert header.lines[-1].startswith("#CHROM")
        assert not any(line.startswith("chr1\t") for line in header.lines)


class TestVcfWindowGrowth:
    def test_grows_window_until_header_complete(self):
        # A header far larger than the initial window.
        contigs = "".join(f"##contig=<ID=c{i},length={i + 1}>\n" for i in range(4000))
        text = f"##fileformat=VCFv4.2\n{contigs}#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\n"
        source = RecordingSource(bgzf_pack(text.encode(), block_size=4096))
        header = read_vcf_header(source, initial_window=1024, max_window=8 << 20)
        assert len(header.contigs) == 4000
        assert header.samples == ["S1"]
        # It must actually have grown rather than guessing right first time.
        assert len(source.requests) > 1
        assert source.requests[0][1] < source.requests[-1][1]

    def test_raises_when_max_window_exceeded(self):
        text = "##fileformat=VCFv4.2\n" + "##contig=<ID=x,length=1>\n" * 5000
        source = BytesByteSource(bgzf_pack(text.encode()))
        with pytest.raises(HeaderError, match="not complete within"):
            read_vcf_header(source, initial_window=256, max_window=512)

    def test_raises_at_end_of_object_without_chrom(self):
        source = BytesByteSource(bgzf_pack(b"##fileformat=VCFv4.2\n"))
        with pytest.raises(HeaderError, match="end of object"):
            read_vcf_header(source, initial_window=64 << 10)


class TestBamHeaderParsing:
    def _pack(self, text: str, refs: list[tuple[str, int]]) -> bytes:
        raw = bytearray(b"BAM\x01")
        encoded = text.encode()
        raw += struct.pack("<i", len(encoded)) + encoded
        raw += struct.pack("<i", len(refs))
        for name, length in refs:
            name_bytes = name.encode() + b"\x00"
            raw += struct.pack("<i", len(name_bytes)) + name_bytes + struct.pack("<i", length)
        return bytes(raw)

    def test_parses_references_and_records(self):
        text = (
            "@HD\tVN:1.6\tSO:coordinate\n"
            "@SQ\tSN:chr1\tLN:1000\n"
            "@RG\tID:s1.u1\tLB:lib1\tPL:ILLUMINA\tSM:SAMPLE_A\n"
            "@PG\tID:bwa\tPN:bwa\tCL:bwa mem -M results/reference/Foo_bar.fa.gz reads.fq\n"
        )
        header = parse_bam_header_bytes(self._pack(text, [("chr1", 1000), ("chr2", 20)]))
        assert header.references == [("chr1", 1000), ("chr2", 20)]
        assert header.sample_names == {"SAMPLE_A"}
        assert header.library_names == {"lib1"}
        assert header.bwa_reference_name == "Foo_bar"

    def test_rejects_bad_magic(self):
        with pytest.raises(HeaderError, match="bad magic"):
            parse_bam_header_bytes(b"SAM\x01rest")

    def test_detects_truncation_within_reference_list(self):
        full = self._pack("@HD\tVN:1.6\n", [("chr1", 1000), ("chr2", 20)])
        with pytest.raises(HeaderError, match="truncated"):
            parse_bam_header_bytes(full[:-6])

    def test_grows_window_for_large_reference_list(self):
        refs = [(f"scaffold_{i}", i + 1) for i in range(3000)]
        packed = bgzf_pack(self._pack("@HD\tVN:1.6\n", refs), block_size=4096)
        source = RecordingSource(packed)
        header = read_bam_header(source, initial_window=1024, max_window=8 << 20)
        assert len(header.references) == 3000
        assert len(source.requests) > 1


class TestRealFixtures:
    def test_podarcis_vcf(self, podarcis_vcf):
        header = read_vcf_header(FileByteSource(podarcis_vcf), initial_window=FIXTURE_WINDOW)
        assert len(header.contigs) == 27
        assert len(header.samples) == 21
        assert header.samples[0] == "SAMN18355762"
        assert header.gatk_argument("sample-ploidy") == "2"
        assert header.gatk_argument("heterozygosity") == "0.005"

    def test_podarcis_bam(self, podarcis_bam):
        header = read_bam_header(FileByteSource(podarcis_bam), initial_window=FIXTURE_WINDOW)
        assert len(header.references) == 27
        assert header.sample_names == {"SAMN18355762"}
        assert header.bwa_reference_name == "Podarcis_raffonei"
        assert header.reference_lengths["CM049750.1"] == 139138986

    def test_grus_bam_has_many_scaffolds(self, grus_bam):
        header = read_bam_header(FileByteSource(grus_bam), initial_window=FIXTURE_WINDOW)
        assert len(header.references) == 930
        assert header.sample_names == {"SAMEA116096693"}

    def test_anser_vcf_carries_the_extra_sample(self, anser_vcf):
        """The sheet omits SAMEA112262514; the VCF is the witness that it ran."""
        header = read_vcf_header(FileByteSource(anser_vcf), initial_window=FIXTURE_WINDOW)
        assert "SAMEA112262514" in header.samples
        assert len(header.samples) == 11


class TestHtslibTrapRegression:
    """Reading one header must never leak into the next.

    htslib caches a remote index in the CWD by basename, and every species'
    VCF is called raw.vcf.gz, so it reported Podarcis contigs inside a
    Catharus header. These assertions encode that this reader cannot.
    """

    def test_no_cross_contamination_between_files(self, podarcis_vcf, anser_vcf):
        first = read_vcf_header(FileByteSource(podarcis_vcf), initial_window=FIXTURE_WINDOW)
        second = read_vcf_header(FileByteSource(anser_vcf), initial_window=FIXTURE_WINDOW)
        again = read_vcf_header(FileByteSource(podarcis_vcf), initial_window=FIXTURE_WINDOW)

        assert not set(first.contigs) & set(second.contigs)
        assert set(again.contigs) == set(first.contigs)
        assert len(second.contigs) == 1337
        # The specific contig that leaked in the observed failure.
        assert "CM049750.1" in first.contigs
        assert "CM049750.1" not in second.contigs

    def test_writes_no_index_files(self, tmp_path, monkeypatch, podarcis_vcf, podarcis_bam):
        monkeypatch.chdir(tmp_path)
        read_vcf_header(FileByteSource(podarcis_vcf), initial_window=FIXTURE_WINDOW)
        read_bam_header(FileByteSource(podarcis_bam), initial_window=FIXTURE_WINDOW)
        assert list(tmp_path.iterdir()) == []
