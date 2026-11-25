"""出方案前对齐页签、接口、控制器和表。只读工作区，不把源码交给模型。"""

import os
import re
from pathlib import Path

SKIP_DIRS = {"node_modules", "dist", "target", ".git", "build", "out", "coverage"}
FILE_LIMIT = 5000
READ_LIMIT = 600_000
MAPPING_RE = re.compile(
    r"@RequestMapping\(\s*(?:(?:value|path)\s*=\s*)?['\"]([^'\"{}]+)['\"]"
)
CLASS_RE = re.compile(r"\bclass\s+(\w+)")
TABLE_RE = re.compile(r"@Table\(\s*(?:value\s*=\s*)?['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]")
LABEL_COMPONENT_RE = re.compile(
    r"label\s*:\s*['\"]([^'\"]+)['\"][\s\S]{0,300}?component\s*:\s*(\w+)"
)
PANE_RE = re.compile(r"<el-tab-pane\b([^>]*)>([\s\S]*?)</el-tab-pane>", re.I)
PANE_LABEL_RE = re.compile(r"""label\s*=\s*['\"]([^'\"]+)['\"]""")
TAG_RE = re.compile(r"<([A-Za-z][\w-]*)\b")
IMPORT_DEFAULT_RE = re.compile(r"import\s+(\w+)\s+from\s+['\"]([^'\"]+)['\"]")
NAMED_IMPORT_RE = re.compile(
    r"import\s+(?:type\s+)?\{([^}]+)\}\s+from\s+['\"]([^'\"]+)['\"]"
)
API_USE_RE = re.compile(r"\bapi\s*:\s*(\w+)")
PATH_RE = re.compile(r"\bpath\s*:\s*['\"](/[^'\"]*)['\"]")
COMPONENT_RE = re.compile(r"\bcomponent\s*:\s*['\"]([^'\"]+)['\"]")
LAZY_RE = re.compile(r"import\(\s*['\"]([^'\"]+)['\"]\s*\)")
SKIP_TAGS = {"template", "div", "span", "slot", "component", "script"}


def norm_mapping(value: str) -> str:
    text = (value or "").strip()
    if not text or any(char in text for char in " \t{}*?"):
        return ""
    if not text.startswith("/"):
        text = "/" + text
    if len(text) > 1:
        text = text.rstrip("/")
    if len(text) < 2:
        return ""
    return text


def _src_root(path: Path) -> Path | None:
    try:
        if path.is_symlink():
            return None
        resolved = path.resolve()
    except OSError:
        return None
    src = resolved / "src"
    if resolved.is_dir() and src.is_dir() and not src.is_symlink():
        return resolved
    return None


def front_root(repo: Path, configured: str = "") -> Path | None:
    """配置了前端仓库就只用它。否则用同级的「仓库名-front」。"""
    if configured:
        return _src_root(Path(configured).expanduser())
    return _src_root(repo.parent / f"{repo.name}-front")


def _read(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > READ_LIMIT:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _iter_files(root: Path, suffixes: set[str]) -> list[Path]:
    found: list[Path] = []
    if not root.is_dir() or root.is_symlink():
        return found
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        if base.is_symlink():
            dirnames[:] = []
            continue
        dirnames[:] = [
            name for name in dirnames
            if name not in SKIP_DIRS and not name.startswith(".")
        ]
        if f"{os.sep}src{os.sep}test{os.sep}" in f"{base}{os.sep}":
            dirnames[:] = []
            continue
        for name in filenames:
            if len(found) >= FILE_LIMIT:
                return found
            path = base / name
            if path.suffix.lower() not in suffixes or path.is_symlink():
                continue
            found.append(path)
    return found


def _tables(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = TABLE_RE.search(line)
        if match is None:
            continue
        window = "\n".join(lines[index:index + 12])
        class_match = CLASS_RE.search(window)
        if class_match is not None:
            found[class_match.group(1)] = match.group(1)
    return found


def _controller(path: Path, text: str, repo: Path) -> dict | None:
    mapping_match = MAPPING_RE.search(text)
    class_match = CLASS_RE.search(text)
    if mapping_match is None or class_match is None:
        return None
    mapping = norm_mapping(mapping_match.group(1))
    if not mapping:
        return None
    try:
        relative = path.resolve().relative_to(repo.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    line = text[:mapping_match.start()].count("\n") + 1
    return {
        "class_name": class_match.group(1),
        "mapping": mapping,
        "source": f"{relative}:{line}",
        "table": "",
    }


def backend_modules(repo: Path) -> list[dict]:
    """仓库里现有的控制器。表名取和类名对得上的实体，不从提交摘录里猜。"""
    entities: dict[str, str] = {}
    controllers: list[dict] = []
    for path in _iter_files(repo, {".java", ".kt"}):
        text = _read(path)
        if not text:
            continue
        if "@Table" in text:
            entities.update(_tables(text))
        if path.name.endswith("Controller.java") or path.name.endswith("Controller.kt"):
            parsed = _controller(path, text, repo)
            if parsed is not None:
                controllers.append(parsed)
    ready = []
    for item in controllers:
        stem = item["class_name"].removesuffix("Controller")
        table = entities.get(stem + "Entity") or entities.get(stem) or ""
        if not table:
            continue
        item["table"] = table
        ready.append(item)
    return ready


def _component_keys(spec: str) -> set[str]:
    text = spec.strip().replace("\\", "/")
    for prefix in ("@/views/", "@/", "src/views/", "views/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.lstrip("/")
    for suffix in (".vue", ".tsx", ".jsx", ".ts", ".js"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    keys = {text}
    if text.endswith("/index"):
        keys.add(text[: -len("/index")])
    return {key.strip("/") for key in keys if key.strip("/")}


def _route_pairs(text: str) -> list[tuple[str, str]]:
    paths = [(match.start(), match.group(1)) for match in PATH_RE.finditer(text)]
    components = [(match.start(), match.group(1)) for match in COMPONENT_RE.finditer(text)]
    components.extend((match.start(), match.group(1)) for match in LAZY_RE.finditer(text))
    pairs = []
    for pos, path in paths:
        near = [
            (abs(comp_pos - pos), spec)
            for comp_pos, spec in components
            if abs(comp_pos - pos) < 600
        ]
        if not near:
            continue
        near.sort()
        pairs.append((path, near[0][1]))
    return pairs


def _page_routes(front: Path) -> dict[str, str]:
    found: dict[str, set[str]] = {}
    for path in _iter_files(front / "src", {".js", ".ts", ".vue"}):
        text = _read(path)
        if "path" not in text or ("component" not in text and "import(" not in text):
            continue
        for route, spec in _route_pairs(text):
            normalized = norm_mapping(route)
            if not normalized:
                continue
            for key in _component_keys(spec):
                found.setdefault(key, set()).add(normalized)
    return {
        key: next(iter(routes)) if len(routes) == 1 else ""
        for key, routes in found.items()
    }


def _resolve(front: Path, importer: Path, spec: str) -> Path | None:
    if spec.startswith("@/"):
        base = front / "src" / spec[2:]
    elif spec.startswith("."):
        base = importer.parent / spec
    else:
        return None
    try:
        base = base.resolve()
        src = (front / "src").resolve()
    except OSError:
        return None
    candidates = [base] if base.suffix else []
    if not base.suffix:
        candidates.extend(Path(str(base) + suffix) for suffix in (".vue", ".ts", ".js", ".tsx", ".jsx"))
        candidates.extend(base / name for name in ("index.vue", "index.ts", "index.js"))
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not str(resolved).startswith(str(src) + os.sep):
            continue
        if resolved.is_symlink() or not resolved.is_file():
            continue
        return resolved
    return None


def _imports(text: str, importer: Path, front: Path) -> dict[str, Path]:
    found = {}
    for match in IMPORT_DEFAULT_RE.finditer(text):
        resolved = _resolve(front, importer, match.group(2))
        if resolved is not None and resolved.suffix == ".vue":
            found[match.group(1)] = resolved
    return found


def _pascal(tag: str) -> str:
    parts = [part for part in tag.split("-") if part]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:1].upper() + parts[0][1:]
    return "".join(part[:1].upper() + part[1:] for part in parts)


def _api_base(text: str) -> str:
    found = re.findall(
        r"baseUrl\s*\(\s*\)\s*\{[\s\S]{0,240}?return\s+['\"](/[^'\"]+)['\"]",
        text,
    )
    if not found:
        found = re.findall(r"baseUrl\s*[:=][^\n]{0,80}?['\"](/[^'\"]+)['\"]", text)
    if not found:
        found = re.findall(r"return\s+['\"](/[^'\"]+)['\"]", text)
    norms = []
    for item in found:
        if "${" in item:
            continue
        mapping = norm_mapping(item)
        if mapping and mapping not in norms:
            norms.append(mapping)
    if not norms:
        return ""
    norms.sort(key=len)
    shortest = norms[0]
    if all(item == shortest or item.startswith(shortest + "/") for item in norms):
        return shortest
    return ""


def _mapping_of_view(front: Path, view: Path) -> str:
    text = _read(view)
    names: dict[str, Path] = {}
    for match in NAMED_IMPORT_RE.finditer(text):
        if match.group(0).startswith("import type"):
            continue
        module = _resolve(front, view, match.group(2))
        if module is None:
            continue
        for piece in match.group(1).split(","):
            piece = piece.strip()
            if not piece or piece.startswith("type "):
                continue
            alias = piece.split(" as ")[-1].strip()
            if alias:
                names[alias] = module
    wanted = [match.group(1) for match in API_USE_RE.finditer(text)]
    modules = []
    for name in wanted:
        module = names.get(name)
        if module is not None and module not in modules:
            modules.append(module)
    if not modules:
        modules = list(dict.fromkeys(names.values()))
    bases = []
    for module in modules:
        base = _api_base(_read(module))
        if base and base not in bases:
            bases.append(base)
    if len(bases) == 1:
        return bases[0]
    return ""


def _filter_labels(view_file: Path) -> list[str]:
    view = view_file.parent
    attrs = view / "attrs"
    if not attrs.is_dir() or attrs.is_symlink():
        return []
    candidates = []
    direct = attrs / "filter-bar-attrs.ts"
    if direct.is_file() and not direct.is_symlink():
        candidates.append(direct)
    for path in sorted(attrs.glob("*.ts")):
        if path not in candidates and not path.is_symlink():
            candidates.append(path)
    labels: list[str] = []
    for path in candidates:
        body = _read(path)
        if "fieldName" not in body:
            continue
        for match in re.finditer(r"label\s*:\s*['\"]([^'\"]+)['\"]", body):
            window = body[match.end():match.end() + 280]
            if re.search(r"fieldName\s*:\s*['\"]", window) is None:
                continue
            label = match.group(1).strip()
            if label and label not in labels:
                labels.append(label)
            if len(labels) == 8:
                return labels
        if labels:
            break
    return labels


def _relative_view(front: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to((front / "src" / "views").resolve())
    except (OSError, ValueError):
        return ""
    return relative.as_posix()


def _route_for(front: Path, host: Path, routes: dict[str, str]) -> str:
    for key in _component_keys(_relative_view(front, host)):
        route = routes.get(key) or ""
        if route:
            return route
    return ""


def _tab_pairs(text: str) -> list[tuple[str, str]]:
    pairs = [
        (label.strip(), component)
        for label, component in LABEL_COMPONENT_RE.findall(text)
        if label.strip()
    ]
    for attrs, inner in PANE_RE.findall(text):
        label_match = PANE_LABEL_RE.search(attrs)
        if label_match is None:
            continue
        label = label_match.group(1).strip()
        component = ""
        for tag in TAG_RE.findall(inner):
            lowered = tag.lower()
            if lowered in SKIP_TAGS or lowered.startswith("el-"):
                continue
            component = _pascal(tag)
            break
        if label and component:
            pairs.append((label, component))
    return pairs


def frontend_tabs(front: Path) -> list[dict]:
    routes = _page_routes(front)
    seen: dict[str, dict | None] = {}
    rows: list[dict] = []
    for path in _iter_files(front / "src", {".vue"}):
        text = _read(path)
        if "label" not in text:
            continue
        imported = _imports(text, path, front)
        if not imported:
            continue
        route = _route_for(front, path, routes)
        for label, component in _tab_pairs(text):
            view = imported.get(component)
            if view is None:
                continue
            mapping = _mapping_of_view(front, view)
            if not mapping:
                continue
            current = seen.get(label, "missing")
            row = {
                "tab": label,
                "route": route,
                "mapping": mapping,
                "filters": _filter_labels(view),
            }
            if current == "missing":
                seen[label] = row
                rows.append(row)
                continue
            if current is None or current["mapping"] != mapping or current["route"] != route:
                seen[label] = None
    return [row for row in rows if seen.get(row["tab"]) is row]


def join_pages(modules: list[dict], tabs: list[dict]) -> list[dict]:
    by_mapping: dict[str, dict] = {}
    ambiguous: set[str] = set()
    for item in modules:
        key = item.get("mapping") or ""
        if not key or key in ambiguous:
            continue
        if key in by_mapping:
            ambiguous.add(key)
            by_mapping.pop(key, None)
            continue
        by_mapping[key] = item
    used: set[str] = set()
    joined: list[dict] = []
    for tab in tabs:
        key = tab.get("mapping") or ""
        if not key or key in ambiguous:
            continue
        if key in used:
            ambiguous.add(key)
            used.discard(key)
            joined = [row for row in joined if row.get("mapping") != key]
            continue
        module = by_mapping.get(key)
        if module is None:
            continue
        used.add(key)
        joined.append({
            "tab": tab.get("tab") or "",
            "route": tab.get("route") or "",
            "mapping": key,
            "class_name": module.get("class_name") or "",
            "table": module.get("table") or "",
            "source": module.get("source") or "",
            "filters": list(tab.get("filters") or []),
        })
    return joined


def build_page_index(repo: str, front_path: str = "") -> list[dict]:
    if not repo:
        return []
    try:
        root = Path(repo).resolve()
    except OSError:
        return []
    if not root.is_dir() or root.is_symlink():
        return []
    front = front_root(root, front_path)
    if front is None:
        return []
    return join_pages(backend_modules(root), frontend_tabs(front))


def use_live_tables(pages: list[dict], catalog: list) -> list[dict]:
    """索引里的注解表名换成库里的真表名。对不上就保持注解名。"""
    live: dict[str, str] = {}
    for item in catalog or []:
        if not isinstance(item, dict):
            continue
        annotation = str(item.get("annotation") or "")
        table = str(item.get("table") or "")
        if annotation and table and annotation not in live:
            live[annotation] = table
    rewritten = []
    for row in pages:
        copied = dict(row)
        annotation = str(copied.get("table") or "")
        if annotation in live:
            copied["table"] = live[annotation]
        rewritten.append(copied)
    return rewritten


def fill_route(plan: dict, pages: list) -> str:
    """页签都落在同一条前端路由上时，补上外层页面路径。已有路径不动。"""
    from qa_platform.runner.menu_page import valid_route

    ui = plan.get("ui") if isinstance(plan.get("ui"), dict) else None
    if not isinstance(ui, dict):
        return ""
    page_list = ui.get("pages") if isinstance(ui.get("pages"), list) else []
    if any(isinstance(page, dict) and valid_route(str(page.get("path") or "")) for page in page_list):
        return ""
    surfaces = ui.get("surfaces") if isinstance(ui.get("surfaces"), list) else []
    by_tab: dict[str, dict] = {}
    for row in pages or []:
        if isinstance(row, dict) and row.get("tab") and row["tab"] not in by_tab:
            by_tab[str(row["tab"])] = row
    routes = []
    for item in surfaces:
        if not isinstance(item, str):
            continue
        row = by_tab.get(item.strip())
        if row is None:
            continue
        route = str(row.get("route") or "")
        if valid_route(route):
            routes.append(route)
    unique = list(dict.fromkeys(routes))
    if len(unique) != 1:
        return ""
    for page in page_list:
        if isinstance(page, dict) and str(page.get("title_text") or "").strip():
            page["path"] = unique[0]
            if not page.get("id"):
                page["id"] = "front-page"
            return unique[0]
    return ""
