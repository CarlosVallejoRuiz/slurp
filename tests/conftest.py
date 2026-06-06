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
def real_graphify_json(tmp_path):
    """Graph in real graphify format: links key, file_type, source_file, confidence."""
    data = {
        "directed": True,
        "multigraph": False,
        "graph": {},
        "nodes": [
            {
                "id": "src/auth/service.py::authenticate_user",
                "label": "authenticate_user",
                "file_type": "function",
                "source_file": "src/auth/service.py",
                "importance": 9,
            },
            {
                "id": "src/middleware/jwt.py::JWTMiddleware",
                "label": "JWTMiddleware",
                "file_type": "class",
                "source_file": "src/middleware/jwt.py",
                "importance": 8,
            },
            {
                "id": "src/auth/utils.py::hash_password",
                "label": "hash_password",
                "file_type": "function",
                "source_file": "src/auth/utils.py",
                "importance": 6,
            },
            {
                "id": "src/models/user.py::UserModel",
                "label": "UserModel",
                "file_type": "class",
                "source_file": "src/models/user.py",
                "importance": 7,
            },
        ],
        "links": [
            {
                "source": "src/middleware/jwt.py::JWTMiddleware",
                "target": "src/auth/service.py::authenticate_user",
                "relation": "calls",
                "weight": 3,
                "confidence": 0.95,
            },
            {
                "source": "src/auth/service.py::authenticate_user",
                "target": "src/auth/utils.py::hash_password",
                "relation": "calls",
                "weight": 2,
                "confidence": 0.90,
            },
            {
                "source": "src/auth/service.py::authenticate_user",
                "target": "src/models/user.py::UserModel",
                "relation": "depends_on",
                "weight": 2,
                "confidence": 0.85,
            },
        ],
        "hyperedges": [],
        "built_at_commit": "abc123def456",
    }
    graph_file = tmp_path / "graphify_real.json"
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
