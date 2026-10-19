"""WalkSearcher: fallback file search when Everything isn't available."""

from __future__ import annotations

from pathlib import Path

from core.file_search import WalkSearcher


def _make_tree(root: Path) -> None:
    """Lay out a tiny game-like folder structure under *root*."""
    (root / "Meshes").mkdir(parents=True)
    (root / "Textures").mkdir(parents=True)
    (root / "Props").mkdir(parents=True)

    (root / "Meshes" / "HeroBody.psk").write_bytes(b"")
    (root / "Meshes" / "HeroHead.pskx").write_bytes(b"")
    (root / "Meshes" / "Crate.PSK").write_bytes(b"")  # case-insensitive ext

    (root / "Textures" / "Hero_C.tga").write_bytes(b"")
    (root / "Textures" / "Hero_N.tga").write_bytes(b"")
    (root / "Textures" / "Hero_C.png").write_bytes(b"")  # different ext, same stem

    (root / "Props" / "Hero.props.txt").write_text("{}", encoding="utf-8")
    (root / "Props" / "HeroHead.props.txt").write_text("{}", encoding="utf-8")


def test_find_psk_files_returns_psk_and_pskx(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    walker = WalkSearcher()
    psk_files = walker.find_psk_files(str(tmp_path))
    names = sorted(p.name for p in psk_files)
    # Casing comes from the filesystem; we just want all three discovered.
    assert {n.lower() for n in names} == {"herobody.psk", "herohead.pskx", "crate.psk"}


def test_find_mesh_files_returns_all_supported_formats(tmp_path: Path) -> None:
    (root := tmp_path / "Exports").mkdir()
    (root / "Body.psk").write_bytes(b"")
    (root / "Head.pskx").write_bytes(b"")
    (root / "Crate.PSK").write_bytes(b"")      # case-insensitive extension
    (root / "Prop.glb").write_bytes(b"")
    (root / "Vehicle.fbx").write_bytes(b"")
    (root / "Deco.gltf").write_bytes(b"")      # not yet supported → excluded
    (root / "Tex.tga").write_bytes(b"")        # non-mesh → excluded

    walker = WalkSearcher()
    meshes = walker.find_mesh_files(str(tmp_path))
    names = {p.name.lower() for p in meshes}
    assert names == {"body.psk", "head.pskx", "crate.psk", "prop.glb", "vehicle.fbx"}


def test_find_mesh_files_rebuilds_when_folder_changes(tmp_path: Path) -> None:
    folder_a = tmp_path / "a"
    folder_b = tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()
    (folder_a / "OnlyA.psk").write_bytes(b"")
    (folder_b / "OnlyB.glb").write_bytes(b"")

    walker = WalkSearcher()
    a_results = walker.find_mesh_files(str(folder_a))
    assert [p.name for p in a_results] == ["OnlyA.psk"]

    # Switching folder must invalidate the cache, not return stale entries.
    b_results = walker.find_mesh_files(str(folder_b))
    assert [p.name for p in b_results] == ["OnlyB.glb"]


def test_find_texture_matches_by_stem_and_extension(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    walker = WalkSearcher()
    matches = walker.find_texture("Hero_C", folder=str(tmp_path))
    # Only the .tga, not the .png — find_texture filters on tga.
    assert len(matches) == 1
    assert matches[0].suffix.lower() == ".tga"
    assert matches[0].stem == "Hero_C"


def test_find_props_file_handles_compound_extension(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    walker = WalkSearcher()
    matches = walker.find_props_file("Hero", folder=str(tmp_path))
    assert len(matches) == 1
    assert matches[0].name == "Hero.props.txt"

    # Different stem → only the matching one is returned.
    head = walker.find_props_file("HeroHead", folder=str(tmp_path))
    assert len(head) == 1
    assert head[0].name == "HeroHead.props.txt"


def test_search_file_with_extension_match(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    walker = WalkSearcher()
    out = walker.search_file("Hero_C", extension="png", folder=str(tmp_path))
    assert len(out) == 1
    assert out[0].suffix == ".png"


def test_index_rebuilds_when_folder_changes(tmp_path: Path) -> None:
    folder_a = tmp_path / "a"
    folder_b = tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()
    (folder_a / "OnlyA.psk").write_bytes(b"")
    (folder_b / "OnlyB.psk").write_bytes(b"")

    walker = WalkSearcher()

    a_results = walker.find_psk_files(str(folder_a))
    assert [p.name for p in a_results] == ["OnlyA.psk"]

    # Switching folder must invalidate the cache, not return stale entries.
    b_results = walker.find_psk_files(str(folder_b))
    assert [p.name for p in b_results] == ["OnlyB.psk"]


def test_test_connection_reports_walker(tmp_path: Path) -> None:
    walker = WalkSearcher()
    ok, msg = walker.test_connection()
    assert ok is True
    assert "walker" in msg.lower() or "everything" in msg.lower()


def test_test_folder_search_reports_count(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    walker = WalkSearcher()
    count, msg = walker.test_folder_search(str(tmp_path))
    assert count > 0
    assert "indexed" in msg.lower()


def test_empty_folder_yields_empty_results(tmp_path: Path) -> None:
    walker = WalkSearcher()
    assert walker.find_psk_files(str(tmp_path)) == []
    assert walker.find_texture("anything", folder=str(tmp_path)) == []
    assert walker.find_props_file("anything", folder=str(tmp_path)) == []
