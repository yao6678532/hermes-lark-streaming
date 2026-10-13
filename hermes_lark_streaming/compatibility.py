"""Read-only source layout resolution; importing Hermes is a separate runtime check.

Follow the composed classes' local imports instead of accepting an unrelated
class with the right method name. Injection still validates each lifecycle seam
and compiles the complete patch plan before any transactional write.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MethodLocation:
    path: Path
    owner: str
    node: ast.FunctionDef | ast.AsyncFunctionDef

    @property
    def parameters(self) -> tuple[str, ...]:
        args = self.node.args
        return tuple(a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs))


@dataclass(frozen=True)
class CompatibilityLayout:
    kind: str
    # Semantic injector role -> actual source path, ordered for existing plans.
    modules: tuple[tuple[str, Path], ...]


def _error(message: str) -> RuntimeError:
    from .patcher import PatcherError

    return PatcherError(f"Compatibility layout: {message}")


class SourceResolver:
    """Resolve methods through reachable local mixins without executing imports."""

    def __init__(self, root: Path, read: Callable[[Path], str]) -> None:
        self.root = root
        self.read = read
        self._trees: dict[Path, ast.Module] = {}
        self._imports: dict[ast.AST, dict[str, set[tuple[Path, str]]]] = {}

    def tree(self, path: Path) -> ast.Module:
        if path not in self._trees:
            if not path.is_file():
                raise _error(f"missing source module {path}")
            try:
                self._trees[path] = ast.parse(self.read(path), filename=str(path))
            except SyntaxError as exc:
                raise _error(f"invalid source {path}: {exc}") from exc
        return self._trees[path]

    def imported(self, scope: ast.AST, symbol: str) -> tuple[Path, str] | None:
        if scope not in self._imports:
            imports: dict[str, set[tuple[Path, str]]] = {}
            for node in ast.walk(scope):
                if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    path = self.root.joinpath(*node.module.split('.')).with_suffix('.py')
                    for alias in node.names:
                        imports.setdefault(alias.asname or alias.name, set()).add((path, alias.name))
            self._imports[scope] = imports
        bindings = self._imports[scope].get(symbol, set())
        if len(bindings) > 1:
            raise _error(f"ambiguous import for {symbol}")
        return next(iter(bindings), None)

    def method(self, path: Path, owner: str, name: str,
               seen: frozenset[tuple[Path, str]] = frozenset()) -> MethodLocation | None:
        key = path, owner
        if key in seen:
            raise _error(f"cyclic mixin inheritance at {path}:{owner}")
        tree = self.tree(path)
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == owner]
        if len(classes) != 1:
            raise _error(f"expected one {owner} class in {path}, found {len(classes)}")
        cls = classes[0]
        own = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
        if len(own) > 1:
            raise _error(f"ambiguous {owner}.{name}")
        if own:
            return MethodLocation(path, owner, own[0])
        found = []
        for base in cls.bases:
            if not isinstance(base, ast.Name):
                raise _error(f"unsupported base expression for {owner}: {ast.unparse(base)}")
            local = any(isinstance(n, ast.ClassDef) and n.name == base.id for n in tree.body)
            binding = (path, base.id) if local else self.imported(tree, base.id)
            # Unavailable unrelated mixins in a sparse source fixture confer no evidence.
            # A required owner still has to be found through an actual declared base.
            if binding and binding[0].is_file():
                location = self.method(*binding, name, seen | {key})
                if location and location not in found:
                    found.append(location)
        if len(found) > 1:
            raise _error(f"ambiguous inherited {owner}.{name}; unsupported MRO")
        return found[0] if found else None

    def require_method(self, path: Path, owner: str, name: str) -> MethodLocation:
        location = self.method(path, owner, name)
        if location is None:
            raise _error(f"cannot resolve {owner}.{name} from {path}")
        return location


def gateway_layout(run_path: Path, read: Callable[[Path], str]) -> CompatibilityLayout:
    resolver = SourceResolver(run_path.parent.parent, read)
    tree = resolver.tree(run_path)
    # Retain the established monolithic contract (its injector validates all hooks).
    if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and n.name == '_handle_message_with_agent' for n in ast.walk(tree)):
        return CompatibilityLayout('monolithic', (('run.py', run_path),))
    composed = any(isinstance(n, ast.ClassDef) and n.name == 'GatewayRunner' and n.bases
                   for n in tree.body)
    if not composed:
        # A damaged legacy target must still be removable; prepare_install fails
        # on its required handler contract before it can write anything.
        return CompatibilityLayout('monolithic', (('run.py', run_path),))
    roles = (
        ('run_inbound.py', '_handle_message'),
        ('run_turn.py', '_handle_message_with_agent'),
        ('run_busy.py', '_busy_stop_command'),
    )
    paths = {role: resolver.require_method(run_path, 'GatewayRunner', method).path
             for role, method in roles}
    build = resolver.require_method(run_path, 'GatewayRunner', '_run_agent_build_turn_context')
    binding = resolver.imported(build.node, 'TurnRunner')
    if binding is None or not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                                  and n.func.id == 'TurnRunner' for n in ast.walk(build.node)):
        raise _error('missing actual TurnRunner construction in turn-context builder')
    runner_path, runner_class = binding
    for method in ('progress_callback', '_setup_stream_consumer', '_wire_turn_agent_callbacks',
                   '_status_callback_sync', '_clarify_callback_sync', 'run_sync'):
        resolver.require_method(runner_path, runner_class, method)
    clarify = resolver.require_method(runner_path, runner_class, '_clarify_callback_sync')
    if clarify.parameters not in (('self', 'question', 'choices', 'multi_select'), ('self', 'questions')):
        raise _error(f'unsupported clarify callback contract: {clarify.parameters}')
    paths['run_turn_runner.py'] = runner_path
    ordered = ('run_inbound.py', 'run_turn.py', 'run_turn_runner.py', 'run_busy.py')
    if len(set(paths.values())) != len(paths):
        raise _error('consolidated role modules require a new injector contract')
    return CompatibilityLayout('modular', tuple((role, paths[role]) for role in ordered))


def cron_layout(path: Path, read: Callable[[Path], str]) -> tuple[Path, bool]:
    resolver = SourceResolver(path.parent.parent, read)
    tree = resolver.tree(path)
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_deliver_result']
    if len(functions) == 1:
        split = any(isinstance(n, ast.FunctionDef) and n.name == '_standalone_send' for n in tree.body)
        return path, split
    binding = resolver.imported(tree, '_deliver_result')
    if not functions and binding is None:
        # Retain recovery/status for an unrecognized legacy file. Its existing
        # delivery contract rejects installation during dry-run verification.
        return path, False
    if functions:
        raise _error(f'cannot resolve cron _deliver_result from {path}')
    assert binding is not None
    target, _symbol = binding
    targets = [n for n in resolver.tree(target).body
               if isinstance(n, ast.FunctionDef) and n.name == '_deliver_result']
    if len(targets) != 1:
        raise _error(f'expected one cron _deliver_result in {target}')
    return target, True
