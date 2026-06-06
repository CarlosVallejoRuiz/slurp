import json

import networkx as nx
import pytest


@pytest.fixture
def sample_graph() -> nx.DiGraph:
    """Sample graph simulating a web project with auth."""
    G = nx.DiGraph()
    nodes = [
        (
            "authenticate_user",
            {
                "label": "authenticate_user",
                "type": "function",
                "description": "Validates user credentials and returns JWT token",
                "importance": 9,
                "file_path": "src/auth/service.py",
            },
        ),
        (
            "JWTMiddleware",
            {
                "label": "JWTMiddleware",
                "type": "class",
                "description": "Intercepts HTTP requests and validates Authorization header",
                "importance": 8,
                "file_path": "src/middleware/jwt.py",
            },
        ),
        (
            "hash_password",
            {
                "label": "hash_password",
                "type": "function",
                "description": "Hashes password using bcrypt",
                "importance": 6,
                "file_path": "src/auth/utils.py",
            },
        ),
        (
            "UserModel",
            {
                "label": "UserModel",
                "type": "class",
                "description": "Database model for user accounts",
                "importance": 7,
                "file_path": "src/models/user.py",
            },
        ),
        (
            "DatabasePool",
            {
                "label": "DatabasePool",
                "type": "class",
                "description": "Manages PostgreSQL connection pool",
                "importance": 5,
                "file_path": "src/db/pool.py",
            },
        ),
        (
            "PaymentService",
            {
                "label": "PaymentService",
                "type": "class",
                "description": "Handles Stripe payment processing",
                "importance": 7,
                "file_path": "src/payments/service.py",
            },
        ),
        (
            "send_email",
            {
                "label": "send_email",
                "type": "function",
                "description": "Sends transactional emails via SendGrid",
                "importance": 4,
                "file_path": "src/notifications/email.py",
            },
        ),
    ]
    for node_id, attrs in nodes:
        G.add_node(node_id, **attrs)

    edges = [
        ("authenticate_user", "hash_password", {"relation": "calls", "weight": 3}),
        ("authenticate_user", "UserModel", {"relation": "depends_on", "weight": 2}),
        ("JWTMiddleware", "authenticate_user", {"relation": "calls", "weight": 3}),
        ("UserModel", "DatabasePool", {"relation": "depends_on", "weight": 2}),
        ("PaymentService", "UserModel", {"relation": "depends_on", "weight": 2}),
        ("PaymentService", "send_email", {"relation": "calls", "weight": 1}),
    ]
    for src, tgt, attrs in edges:
        G.add_edge(src, tgt, **attrs)

    return G


@pytest.fixture
def sample_graph_json(tmp_path, sample_graph: nx.DiGraph):
    """Saves the sample graph as graph.json and returns the path."""
    data = {
        "nodes": [{"id": n, **sample_graph.nodes[n]} for n in sample_graph.nodes],
        "edges": [
            {"source": u, "target": v, **sample_graph.edges[u, v]}
            for u, v in sample_graph.edges
        ],
    }
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps(data))
    return graph_file


@pytest.fixture
def generic_graph_json(tmp_path) -> object:
    """Graph in generic format (only id, label, source, target)."""
    data = {
        "nodes": [
            {"id": "a", "label": "NodeA"},
            {"id": "b", "label": "NodeB"},
            {"id": "c", "label": "NodeC"},
        ],
        "edges": [
            {"source": "a", "target": "b"},
            {"source": "b", "target": "c"},
        ],
    }
    graph_file = tmp_path / "generic.json"
    graph_file.write_text(json.dumps(data))
    return graph_file
