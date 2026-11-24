"""只读 SQL 的词法检查。字符串里的内容不当关键字。"""


class GrammarError(ValueError):
    pass


READ_VERBS = {"SELECT", "SHOW", "EXPLAIN"}


def tokenize(sql: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if char == "-" and index + 1 < length and sql[index + 1] == "-":
            index += 2
            while index < length and sql[index] != "\n":
                index += 1
            continue
        if char == "#":
            index += 1
            while index < length and sql[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and sql[index + 1] == "*":
            index += 2
            while index + 1 < length and not (sql[index] == "*" and sql[index + 1] == "/"):
                index += 1
            if index + 1 >= length:
                raise GrammarError("注释没有结束")
            index += 2
            continue
        if char == "'":
            index += 1
            while index < length:
                if sql[index] == "\\" and index + 1 < length:
                    index += 2
                    continue
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise GrammarError("字符串没有结束")
            tokens.append("STRING")
            continue
        if char in {'"', "`"}:
            quote = char
            index += 1
            while index < length and sql[index] != quote:
                if sql[index] == "\\" and index + 1 < length:
                    index += 2
                    continue
                index += 1
            if index >= length:
                raise GrammarError("标识符没有结束")
            index += 1
            tokens.append("IDENT")
            continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < length and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            tokens.append(sql[index:end].upper())
            index = end
            continue
        if char.isdigit():
            end = index + 1
            while end < length and (sql[end].isdigit() or sql[end] == "."):
                end += 1
            tokens.append("NUM")
            index = end
            continue
        tokens.append(char)
        index += 1
    return tokens


def _skip_paren(tokens: list[str], index: int) -> int:
    depth = 0
    while index < len(tokens):
        if tokens[index] == "(":
            depth += 1
        elif tokens[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise GrammarError("括号没有结束")


def _after_cte(tokens: list[str]) -> int:
    index = 1
    if index < len(tokens) and tokens[index] == "RECURSIVE":
        index += 1
    while index < len(tokens):
        index += 1
        if index < len(tokens) and tokens[index] == "AS":
            index += 1
        if index >= len(tokens) or tokens[index] != "(":
            raise GrammarError("CTE 缺少子查询")
        index = _skip_paren(tokens, index)
        if index < len(tokens) and tokens[index] == ",":
            index += 1
            continue
        return index
    raise GrammarError("CTE 后面没有主语句")


def _statement(tokens: list[str]) -> list[str]:
    if not tokens:
        raise GrammarError("空语句")
    semis = [index for index, token in enumerate(tokens) if token == ";"]
    if not semis:
        return tokens
    if len(semis) > 1 or semis[0] != len(tokens) - 1:
        raise GrammarError("只能有一条语句")
    body = tokens[:-1]
    if not body:
        raise GrammarError("空语句")
    return body


def _banned(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if token == "INTO":
            return True
        if token == "FOR" and index + 1 < len(tokens) and tokens[index + 1] == "UPDATE":
            return True
        if tokens[index:index + 4] == ["LOCK", "IN", "SHARE", "MODE"]:
            return True
        if token == "LOAD_FILE" and index + 1 < len(tokens) and tokens[index + 1] == "(":
            return True
    return False


def main_verb(sql: str) -> str:
    body = _statement(tokenize(sql))
    index = _after_cte(body) if body[0] == "WITH" else 0
    if index >= len(body):
        raise GrammarError("没有主动词")
    verb = body[index]
    if verb in {"STRING", "IDENT", "NUM", "(", ")", ",", ";"}:
        raise GrammarError("没有主动词")
    return verb


def assert_read_only(sql: str) -> None:
    tokens = tokenize(sql)
    if _banned(tokens):
        raise GrammarError("只读语句被拒绝")
    if main_verb(sql) not in READ_VERBS:
        raise GrammarError("只读语句被拒绝")
