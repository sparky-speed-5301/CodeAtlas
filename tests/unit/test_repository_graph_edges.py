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

    source = next(item for item in restored.symbols if item.file == "pkg/main.py" and item.name == "run")
    target = next(item for item in restored.symbols if item.file == "pkg/helper.py" and item.name == "helper")
    paths = graph.get_bounded_cross_file_paths(source.id, target.id)
    assert len(paths) == 1
    assert paths[0][0].resolution_state == "resolved"
    assert paths[0][0].source_file == "pkg/main.py"
    assert paths[0][0].target_file == "pkg/helper.py"


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


def test_bounded_cross_file_paths_are_cycle_safe_and_limitable() -> None:
    from codeatlas.repository.models import Symbol

    symbols = [
        Symbol(id="sym::a", file="a.py", name="a", kind="function", start_line=1, end_line=1),
        Symbol(id="sym::b", file="b.py", name="b", kind="function", start_line=1, end_line=1),
        Symbol(id="sym::c", file="c.py", name="c", kind="function", start_line=1, end_line=1),
    ]

    def edge(source: str, target: str, source_file: str, target_file: str) -> GraphEdge:
        return GraphEdge(
            edge_id=GraphEdge.make_id(
                edge_type="reference",
                source_file=source_file,
                target_file=target_file,
                source_symbol_id=source,
                target_symbol_id=target,
                source_line=1,
            ),
            edge_type="reference",
            source_file=source_file,
            target_file=target_file,
            source_symbol_id=source,
            target_symbol_id=target,
            source_line=1,
            evidence_kind="symbol_reference",
            resolution_state="resolved",
        )

    graph = DependencyGraph(
        [],
        [],
        symbols,
        [edge("sym::a", "sym::b", "a.py", "b.py"), edge("sym::b", "sym::c", "b.py", "c.py"), edge("sym::c", "sym::a", "c.py", "a.py")],
        GraphLimits(max_traversal_depth=1, max_flow_paths=1),
    )
    assert graph.get_bounded_cross_file_paths("sym::a", "sym::c") == []
    assert graph.get_bounded_cross_file_paths("sym::a", "sym::b")


def test_relationship_edges_cover_inheritance_routes_exports_and_configuration(tmp_path) -> None:
    (tmp_path / "app.py").write_text(
        "import os\n\nclass Base:\n    pass\n\nclass Child(Base):\n    pass\n\n@app.route('/items')\ndef items():\n    return os.getenv('TOKEN')\n",
        encoding="utf-8",
    )
    index = build_repository_index(tmp_path)
    edge_types = {edge.edge_type for edge in index.graph_edges}

    assert {"inheritance", "route_handler", "export", "configuration_key"} <= edge_types
    inheritance = next(edge for edge in index.graph_edges if edge.edge_type == "inheritance")
    assert inheritance.resolution_state == "resolved"
    assert inheritance.target_symbol_id == "sym::app.py::class::root::Base"
    route = next(edge for edge in index.graph_edges if edge.edge_type == "route_handler")
    assert route.evidence_kind == "route_declaration"
    config = next(edge for edge in index.graph_edges if edge.edge_type == "configuration_key")
    assert config.evidence_kind == "configuration_declaration"
    assert "TOKEN" not in config.model_dump_json()
