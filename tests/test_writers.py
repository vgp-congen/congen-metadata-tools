"""Tests for the metadata writers.

These matter for `congen readme`, which will write into congen-metadata:
comments must survive, and regeneration must be byte-stable.
"""

from __future__ import annotations

import pytest

from congen.core.metadata.writers import (
    ManagedBlock,
    atomic_write,
    dump_yaml_preserving,
    dump_yaml_roundtrip,
    load_yaml_roundtrip,
    managed_block_names,
    render_csv,
    render_managed_blocks,
    restore_bool_casing,
    would_change,
    write_if_changed,
)

COMMENTED_YAML = """\
# snpArcher v2 Configuration Example

samples: "config/sample_sheet.csv"

reference:
  name: "Podarcis_raffonei"
  source: "GCA_027172205.1"  # accession, url, or path

callable_sites:
  generate_bed_file: True
  coverage:
    enabled: true
"""


class TestYamlRoundTrip:
    def test_comments_and_quotes_survive(self):
        data = load_yaml_roundtrip(COMMENTED_YAML)
        out = dump_yaml_preserving(COMMENTED_YAML, data)
        assert out == COMMENTED_YAML

    def test_edited_value_produces_a_minimal_diff(self):
        data = load_yaml_roundtrip(COMMENTED_YAML)
        data["reference"]["source"] = "GCA_052056855.1"
        out = dump_yaml_preserving(COMMENTED_YAML, data)
        changed = [
            (a, b)
            for a, b in zip(COMMENTED_YAML.splitlines(), out.splitlines())
            if a != b
        ]
        assert len(changed) == 1
        assert "GCA_052056855.1" in changed[0][1]
        # The inline comment on that line is preserved.
        assert "# accession, url, or path" in changed[0][1]

    def test_mixed_boolean_casing_is_preserved(self):
        """ruamel lowercases booleans; these configs mix True and true."""
        naive = dump_yaml_roundtrip(load_yaml_roundtrip(COMMENTED_YAML))
        assert "generate_bed_file: true" in naive  # ruamel normalized it
        preserved = dump_yaml_preserving(COMMENTED_YAML, load_yaml_roundtrip(COMMENTED_YAML))
        assert "generate_bed_file: True" in preserved
        assert "enabled: true" in preserved

    def test_bool_casing_left_alone_when_original_is_inconsistent(self):
        original = "a:\n  x: True\nb:\n  x: true\n"
        # Same key prefix ("  x: ") with two spellings: no unambiguous choice.
        out = restore_bool_casing(original, "a:\n  x: true\nb:\n  x: true\n")
        assert out == "a:\n  x: true\nb:\n  x: true\n"

    def test_restore_handles_trailing_comments(self):
        original = "flag: True  # why\n"
        out = restore_bool_casing(original, "flag: true  # why\n")
        assert out == original

    def test_no_booleans_is_a_passthrough(self):
        assert restore_bool_casing("a: 1\n", "a: 1\n") == "a: 1\n"

    @pytest.mark.parametrize("species", [
        "reptiles/podarcis-raffonei",
        "birds/anser-albifrons",
        "birds/sturnus-vulgaris",
        "birds/grus-americana",
    ])
    def test_real_configs_round_trip_byte_identically(self, metadata_root, species):
        path = metadata_root / "species" / species / "config.yaml"
        source = path.read_text()
        assert dump_yaml_preserving(source, load_yaml_roundtrip(source)) == source


class TestAtomicWriteAndChangeDetection:
    def test_would_change_on_missing_file(self, tmp_path):
        assert would_change(tmp_path / "new.md", "x")

    def test_would_change_compares_content(self, tmp_path):
        path = tmp_path / "a.md"
        atomic_write(path, "same")
        assert not would_change(path, "same")
        assert would_change(path, "different")

    def test_write_if_changed_reports_whether_it_wrote(self, tmp_path):
        path = tmp_path / "a.md"
        assert write_if_changed(path, "one") is True
        assert write_if_changed(path, "one") is False
        assert write_if_changed(path, "two") is True
        assert path.read_text() == "two"

    def test_atomic_write_creates_parents_and_leaves_no_temp(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "a.md"
        atomic_write(path, "hello")
        assert path.read_text() == "hello"
        assert [p.name for p in path.parent.iterdir()] == ["a.md"]

    def test_failed_write_leaves_original_intact(self, tmp_path, monkeypatch):
        path = tmp_path / "a.md"
        atomic_write(path, "original")

        import os as os_module

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(os_module, "replace", boom)
        with pytest.raises(OSError):
            atomic_write(path, "replacement")
        assert path.read_text() == "original"
        assert [p.name for p in tmp_path.iterdir()] == ["a.md"]


class TestManagedBlocks:
    def test_appends_a_missing_block(self):
        out = render_managed_blocks("# Title\n", [ManagedBlock("stats", "n = 21")])
        assert "<!-- congen:begin stats -->" in out
        assert "n = 21" in out
        assert out.startswith("# Title\n")

    def test_replaces_only_block_content(self):
        existing = (
            "# Title\n\nHand-written prose that must survive.\n\n"
            "<!-- congen:begin stats -->\nold\n<!-- congen:end stats -->\n\nMore prose.\n"
        )
        out = render_managed_blocks(existing, [ManagedBlock("stats", "new")])
        assert "Hand-written prose that must survive." in out
        assert "More prose." in out
        assert "new" in out
        assert "old" not in out

    def test_is_idempotent(self):
        blocks = [ManagedBlock("stats", "n = 21")]
        once = render_managed_blocks("# T\n", blocks)
        assert render_managed_blocks(once, blocks) == once

    def test_multiple_blocks_are_independent(self):
        out = render_managed_blocks(
            "# T\n", [ManagedBlock("a", "A"), ManagedBlock("b", "B")]
        )
        out = render_managed_blocks(out, [ManagedBlock("a", "A2")])
        assert "A2" in out and "B" in out and "A\n" not in out.replace("A2", "")

    def test_empty_content_leaves_an_empty_block(self):
        out = render_managed_blocks("# T\n", [ManagedBlock("stats", "")])
        assert "<!-- congen:begin stats -->\n<!-- congen:end stats -->" in out

    def test_block_names_are_discoverable(self):
        out = render_managed_blocks("", [ManagedBlock("a", "1"), ManagedBlock("b", "2")])
        assert managed_block_names(out) == ["a", "b"]


class TestRenderCsv:
    def test_preserves_line_terminator(self):
        out = render_csv(
            ["sample_id", "input_type", "input"],
            [{"sample_id": "S1", "input_type": "srr", "input": "SRR1"}],
            line_terminator="\r\n",
        )
        assert out == "sample_id,input_type,input\r\nS1,srr,SRR1\r\n"

    def test_missing_keys_become_empty(self):
        out = render_csv(["a", "b"], [{"a": "1"}])
        assert out == "a,b\n1,\n"

    def test_unknown_keys_are_ignored(self):
        out = render_csv(["a"], [{"a": "1", "z": "9"}])
        assert out == "a\n1\n"
