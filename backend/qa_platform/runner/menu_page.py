"""方案没写页面路径时，从菜单表里补一条。不改已确认方案。"""

import re

from qa_platform.runner.db import QueryError, query_script, read_only_query

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def valid_route(path: str) -> bool:
    return bool(path) and path.startswith("/") and not path.startswith("//") and "://" not in path


def menu_table_sql(database: str) -> str:
    """同时有菜单名、路径、组件这三列的表。正好一张才用。"""
    return (
        "SELECT TABLE_NAME FROM information_schema.COLUMNS "
        f"WHERE TABLE_SCHEMA = '{database}' "
        "AND COLUMN_NAME IN ('menu_name', 'route', 'component') "
        "GROUP BY TABLE_NAME "
        "HAVING COUNT(DISTINCT COLUMN_NAME) = 3 "
        "LIMIT 5"
    )


def menu_rows_sql(table: str) -> str:
    return (
        f"SELECT menu_name, route, component FROM `{table}` "
        "WHERE route IS NOT NULL AND route <> '' "
        "AND component IS NOT NULL AND component <> '' LIMIT 300"
    )


def menu_by_name(name: str, rows: list) -> dict | None:
    """页面名和菜单名完全相同，并且只对应一条路径时才用。"""
    wanted = (name or "").strip()
    if not wanted:
        return None
    found: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("menu_name") or row.get("name") or "").strip()
        route = str(row.get("route") or row.get("path") or "").strip()
        component = str(row.get("component") or "").strip()
        if label != wanted or not component or not valid_route(route):
            continue
        found.setdefault(route, {"name": label, "path": route, "component": component})
    if len(found) != 1:
        return None
    return next(iter(found.values()))


def choose_menu_page(text: str, rows: list) -> dict | None:
    """菜单名原样出现在需求正文里才采用。对上多条就不猜。"""
    body = text or ""
    found: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("menu_name") or row.get("name") or "").strip()
        route = str(row.get("route") or row.get("path") or "").strip()
        component = str(row.get("component") or "").strip()
        if not name or not component or not valid_route(route):
            continue
        if name not in body:
            continue
        found.setdefault(route, {"name": name, "path": route, "component": component})
    if len(found) != 1:
        return None
    return next(iter(found.values()))


def _table_names(rows) -> list[str]:
    names = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("TABLE_NAME") or "").strip()
        if IDENT.fullmatch(name) and name not in names:
            names.append(name)
    return names


def fetch_menus(database: str, query) -> list[dict]:
    """query(database, sql, allowed) 由调用方提供。没有唯一的菜单表就返回空。"""
    if IDENT.fullmatch(database or "") is None or query is None:
        return []
    try:
        found = query(database, menu_table_sql(database), [database])
    except (QueryError, OSError, TypeError):
        return []
    names = _table_names(found if isinstance(found, list) else [])
    if len(names) != 1:
        return []
    try:
        rows = query(database, menu_rows_sql(names[0]), [database])
    except (QueryError, OSError, TypeError):
        return []
    return rows if isinstance(rows, list) else []


def load_menus(database: str) -> list[dict]:
    if IDENT.fullmatch(database or "") is None or not query_script().is_file():
        return []
    return fetch_menus(
        database,
        lambda name, sql, allowed: read_only_query(name, sql, allowed=allowed),
    )


def pick_path(plan: dict, text: str, rows: list) -> tuple[str | None, str]:
    """已有路径就用它。没有时用页面名对菜单，再不改方案文件。"""
    ui = plan.get("ui") if isinstance(plan.get("ui"), dict) else {}
    pages = ui.get("pages") if isinstance(ui.get("pages"), list) else []
    for page in pages:
        if isinstance(page, dict) and valid_route(str(page.get("path") or "")):
            return str(page["path"]), ""
    for page in pages:
        if not isinstance(page, dict):
            continue
        chosen = menu_by_name(str(page.get("title_text") or ""), rows)
        if chosen is not None:
            return chosen["path"], f"方案没有页面路径，按菜单「{chosen['name']}」打开 {chosen['path']}"
    chosen = choose_menu_page(text, rows)
    if chosen is None:
        return None, ""
    return chosen["path"], f"方案没有页面路径，按菜单「{chosen['name']}」打开 {chosen['path']}"


def attach_page(plan: dict, text: str, rows: list) -> dict | None:
    """只在还没有路径时补上。页面名对得上唯一菜单就写入 path，不改页面名。"""
    ui = plan.get("ui")
    if not isinstance(ui, dict):
        ui = {}
        plan["ui"] = ui
    pages = ui.get("pages")
    if isinstance(pages, list):
        if any(isinstance(page, dict) and valid_route(str(page.get("path") or "")) for page in pages):
            return None
        titled = [
            page for page in pages
            if isinstance(page, dict) and str(page.get("title_text") or "").strip()
        ]
        if titled:
            filled = None
            for page in titled:
                chosen = menu_by_name(str(page.get("title_text") or ""), rows)
                if chosen is None:
                    continue
                page["path"] = chosen["path"]
                if not page.get("id"):
                    page["id"] = "menu-page"
                filled = chosen
            return filled
    chosen = choose_menu_page(text, rows)
    if chosen is None:
        return None
    ui["pages"] = [{
        "id": "menu-page",
        "path": chosen["path"],
        "title_text": chosen["name"],
        "controls": [],
    }]
    return chosen
