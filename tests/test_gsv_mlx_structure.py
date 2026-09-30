from tools.probes.gsv_mlx_structure import graph_operations


def test_graph_counts_primitives_not_arrays_edges_or_labels():
    dot = '''digraph {
{ rank=source; "A"; }
{ 42 [label ="Matmul", shape=rectangle]; }
"A" -> 42
42 -> "B"
{ 43 [label ="Matmul", shape=rectangle]; }
{ 44 [label ="Add", shape=rectangle]; }
{ rank=sink; "Matmul"; }
}'''
    assert graph_operations(dot) == {"Add": 1, "Matmul": 2}
