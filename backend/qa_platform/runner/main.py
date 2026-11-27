"""产品执行器入口。Git 前置之后读取需求正文。"""

import argparse
import fcntl
import sys
from pathlib import Path

from qa_platform.runner.baseline import continue_baseline, plan_is_approved
from qa_platform.runner.commits import continue_plan
from qa_platform.runner.db import continue_apply, read_only_query
from qa_platform.runner.execute import execute_reuse, execute_sql_tests, page_seed_ready, reuse_ready, sql_test_ready
from qa_platform.runner.preflight import execute, note, record
from qa_platform.runner.requirement import fetch as fetch_requirement
from qa_platform.store import read_state


def read_secrets() -> tuple[str, str]:
    deepseek = sys.stdin.readline().rstrip("\r\n")
    yunxiao = sys.stdin.readline().rstrip("\r\n")
    return deepseek, yunxiao


def hold_lock(run_dir: Path):
    handle = (run_dir / ".run.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        try:
            record(
                run_dir,
                exit_code=2,
                event_status="阻塞",
                basis="已有测试在运行，请等当前运行结束",
            )
        except OSError:
            pass
        raise
    return handle


def finish_observed(run_dir: Path, code: int) -> int:
    """这一轮执行结束且没有暂停时封口。报告渲染不再改用例状态。"""
    if code != 0:
        return code
    from qa_platform.runner.report import render_report, seal_run, unsealed_summary

    seal_run(run_dir)
    report = render_report(run_dir)
    if report is not None:
        record(run_dir, exit_code=0, event_status="完成", basis="报告已生成", step="执行")
        return code
    record(
        run_dir, exit_code=2, event_status="阻塞",
        basis="报告未生成：" + unsealed_summary(run_dir), step="执行",
    )
    return 2


def _run_locked(run_dir: Path, args) -> int:
    state = read_state(run_dir)
    if state.get("review"):
        if state.get("exit_code") != 4:
            record(
                run_dir, exit_code=4, event_status="待确认",
                basis="已有待确认项，未读取手工提交", step="方案",
            )
        return 4
    if plan_is_approved(run_dir, state):
        from qa_platform.runner.baseline import _load_plan
        from qa_platform.runner.data_plan import ensure_assessed, has_data_plan

        plan = _load_plan(run_dir) or {}
        if has_data_plan(plan):
            code = ensure_assessed(run_dir)
            if code != 0:
                return code
            note(run_dir, step="执行", basis="正在执行已经备好数据的用例")
            deepseek, _yunxiao = read_secrets()
            from qa_platform.runner.execute import deepseek_model, execute_data_plan
            from qa_platform.runner.ocu import ProductSession
            product = ProductSession()
            try:
                code = execute_data_plan(
                    run_dir,
                    session=product,
                    model=deepseek_model(deepseek),
                    query=lambda database, sql, allowed: read_only_query(
                        database, sql, allowed=allowed, run_dir=run_dir,
                    ),
                )
            finally:
                product.close()
            return finish_observed(run_dir, code)
        from qa_platform.runner.db import _load_baseline
        current = _load_baseline(run_dir) or {}
        if current.get("strategy") == "sql":
            if not args.apply_now and sql_test_ready(run_dir, state):
                note(run_dir, step="执行", basis="正在执行测试步骤")
                deepseek, _yunxiao = read_secrets()
                from qa_platform.runner.execute import deepseek_model
                from qa_platform.runner.ocu import ProductSession
                product = ProductSession()
                try:
                    code = execute_sql_tests(
                        run_dir,
                        session=product,
                        model=deepseek_model(deepseek),
                        query=lambda database, sql, allowed: read_only_query(
                            database, sql, allowed=allowed, run_dir=run_dir,
                        ),
                    )
                finally:
                    product.close()
                return finish_observed(run_dir, code)
            note(run_dir, step="改数", basis="正在核对改数结果")
            return continue_apply(run_dir, apply_now=bool(args.apply_now))
        if page_seed_ready(run_dir, state):
            record(run_dir, exit_code=2, event_status="阻塞", basis="数据库只能查询，未做页面铺垫", step="造数")
            return 2
        if reuse_ready(run_dir, state):
            note(run_dir, step="执行", basis="正在执行测试步骤")
            deepseek, _yunxiao = read_secrets()
            from qa_platform.runner.execute import deepseek_model
            from qa_platform.runner.ocu import ProductSession
            product = ProductSession()
            try:
                code = execute_reuse(
                    run_dir,
                    session=product,
                    model=deepseek_model(deepseek),
                    query=lambda database, sql, allowed: read_only_query(
                        database, sql, allowed=allowed, run_dir=run_dir,
                    ),
                )
            finally:
                product.close()
            return finish_observed(run_dir, code)
        note(run_dir, step="基线", basis="正在做只读基线")
        return continue_baseline(run_dir)
    deepseek, yunxiao = read_secrets()
    if not state.get("commit"):
        note(run_dir, step="前置检查", basis="正在核对本地仓库和分支")
        code = execute(run_dir)
        if code != 0:
            return code
        state = read_state(run_dir)
    if not isinstance(state.get("requirement"), dict):
        note(run_dir, step="需求正文", basis="正在读取云效需求")
        code = fetch_requirement(run_dir, yunxiao)
        if code != 0:
            return code
    note(run_dir, step="方案", basis="正在查找提交并生成方案")
    return continue_plan(run_dir, deepseek)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--apply-now", action="store_true")
    args = parser.parse_args(argv)
    run_dir = Path(args.out)
    for name in ("stop.requested", "continue.requested"):
        marker = run_dir / name
        if marker.is_file() and not marker.is_symlink():
            marker.unlink()
    try:
        lock = hold_lock(run_dir)
    except BlockingIOError:
        return 2
    try:
        try:
            return _run_locked(run_dir, args)
        except Exception:
            record(run_dir, exit_code=2, event_status="阻塞", basis="执行中断，没有写回页面", step="执行")
            raise
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BlockingIOError:
        raise SystemExit(2)
