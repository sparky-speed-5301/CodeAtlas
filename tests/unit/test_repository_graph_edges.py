from __future__ import annotations

from codeatlas.repository import DependencyGraph, GraphEdge, GraphLimits, build_repository_index, is_index_fresh


def test_graph_edges_round_trip_with_resolved_unresolved_and_containment(tmp_path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "main.py").write_text(
        "from .helper import helper\n\nclass Runner:\n    def run(self):\n        return helper()\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "dynamic.py").write_text(
        "module = __import__(name_from_config)\n", encoding="utf-8"
    )

    index = build_repository_index(tmp_path)
    edge_types = {edge.edge_type for edge in index.graph_edges}

    assert "import" in edge_types
    assert "containment" in edge_types
    assert all(edge.edge_id.startswith("edge::") for edge in index.graph_edges)
    assert all("return 1" not in edge.model_dump_json() for edge in index.graph_edges)

    serialized = index.model_dump_json()
    restored = type(index).model_validate_json(serialized)
    assert restored.graph_edges == index.graph_edges

    graph = DependencyGraph(restored.imports, restored.references, restored.symbols, restored.graph_edges)
    assert graph.get_import_edges("pkg/main.py")
    assert graph.get_reference_edges("pkg/main.py")


def test_graph_bounds_emit_diagnostic_and_truncate(tmp_path) -> None:
    (tmp_path / "main.py").write_text(
        "import os\nimport sys\n\ndef one():\n    return 1\n",
        encoding="utf-8",
    )
    index = build_repository_index(tmp_path, config={"max_graph_edges": 1})

    assert len(index.graph_edges) == 1
    assert any(item.reason == "graph_bounds_exceeded" for item in index.diagnostics)


def test_graph_edge_ids_include_source_line() -> None:
    first = GraphEdge.make_id(
        edge_type="reference",
        source_file="main.py",
        target_file="helper.py",
        source_symbol_id=None,
        target_symbol_id="sym::helper",
        source_line=3,
    )
    second = GraphEdge.make_id(
        edge_type="reference",
        source_file="main.py",
        target_file="helper.py",
        source_symbol_id=None,
        target_symbol_id="sym::helper",
        source_line=4,
    )
    assert first != second


def test_graph_freshness_includes_optional_source_config_and_parser_identity(tmp_path) -> None:
    (tmp_path / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    index = build_repository_index(tmp_path, commit_sha="a" * 40)

    assert index.source_inventory_sha256 is not None
    assert index.graph_config_sha256 is not None
    assert is_index_fresh(index, "a" * 40)
    assert is_index_fresh(
        index,
        "a" * 40,
        source_inventory_sha256=index.source_inventory_sha256,
        graph_config_sha256=index.graph_config_sha256,
        parser_version=index.parser_version,
    )
    assert not is_index_fresh(index, "a" * 40, parser_version="different")
    assert not is_index_fresh(index, "a" * 40, graph_config_sha256="b" * 64)


def test_graph_limits_are_serialized_and_configurable(tmp_path) -> None:
    (tmp_path / "main.py").write_text("import os\nimport sys\n", encoding="utf-8")
    limits = GraphLimits(max_edges=1, max_traversal_depth=2)
    index = build_repository_index(tmp_path, config={"graph_limits": limits.model_dump(mode="json")})

    assert index.graph_limits.max_edges == 1
    assert len(index.graph_edges) <= 1
    restored = type(index).model_validate_json(index.model_dump_json())
    assert restored.graph_limits == index.graph_limits
