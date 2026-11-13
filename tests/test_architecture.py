"""Keep functional ownership and module boundaries explicit."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "projecthub"


def imports(nodes):
    for node in nodes:
        if isinstance(node, ast.If) and ast.unparse(node.test) in ("TYPE_CHECKING", "typing.TYPE_CHECKING"):
            continue
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module
        elif isinstance(node, ast.Import):
            yield from (item.name for item in node.names)
        yield from imports(ast.iter_child_nodes(node))


def test_production_features_are_packages():
    assert [p.name for p in ROOT.glob("*.py")] == ["__init__.py"]
    assert all(
        (p / "__init__.py").is_file() for p in ROOT.iterdir() if p.is_dir() and p.name != "__pycache__"
    )


def test_modules_use_public_interfaces_without_dependency_cycles():
    graph = {p.name: set() for p in ROOT.iterdir() if p.is_dir() and (p / "__init__.py").is_file()}
    for path in ROOT.rglob("*.py"):
        owner = path.relative_to(ROOT).parts[0]
        if owner not in graph:
            continue
        for name in imports(ast.parse(path.read_text(encoding="utf-8-sig")).body):
            parts = name.split(".")
            if len(parts) >= 2 and parts[0] == "projecthub" and parts[1] != owner:
                assert len(parts) == 2, f"{path}: use the {parts[1]} package public interface"
                graph[owner].add(parts[1])

    def visit(name, active, done):
        assert name not in active, "module dependency cycle: " + " -> ".join([*active, name])
        if name in done:
            return
        for dependency in graph[name]:
            visit(dependency, [*active, name], done)
        done.add(name)

    done = set()
    for name in graph:
        visit(name, [], done)


def test_accounts_and_files_do_not_own_project_or_permission_interfaces():
    from projecthub.files import FileService
    from projecthub.permissions import Permissions
    from projecthub.projects import ProjectService
    from projecthub.users import Users

    assert not hasattr(Users, "require")
    assert not hasattr(Users, "get_project_version")
    assert not hasattr(FileService, "update_project")
    assert callable(Permissions.require)
    assert callable(ProjectService.update_project)
