"""数据库迁移框架的结构约束。

迁移是在升级时对存量生产库执行的 SQL，一旦写错无法回退，因此这里用测试
把「约定」固化下来：版本必须可排序、语句不能为空、已发布版本不得被改写。
真正的执行幂等性依赖 PostgreSQL（ALTER TABLE ... IF NOT EXISTS），
在 CI 的 postgres service 环境中由 tests/test_migrations_pg.py 覆盖。
"""
import pytest

from app.migrations import MIGRATIONS, _MIGRATION_ORDER
from app.version import parse_version


def test_versions_are_sortable_triples():
    for version in MIGRATIONS:
        parsed = parse_version(version)
        assert len(parsed) == 3
        assert all(isinstance(p, int) for p in parsed)


def test_migration_order_matches_semver():
    """执行顺序必须与版本号升序一致，否则新迁移可能先于旧迁移执行。"""
    assert _MIGRATION_ORDER == sorted(MIGRATIONS.keys(), key=parse_version)


def test_every_migration_has_statements():
    for version, statements in MIGRATIONS.items():
        assert statements, f"{version} 没有语句"
        assert isinstance(statements, list)
        for sql in statements:
            assert isinstance(sql, str)
            assert sql.strip(), f"{version} 含空语句"


def test_new_alert_feature_migration_present():
    """4.6.0 是本轮告警能力增强的迁移，必须包含全部新增列。"""
    assert "4.6.0" in MIGRATIONS
    joined = "\n".join(MIGRATIONS["4.6.0"])

    for column in (
        "mode",
        "baseline_sigma",
        "baseline_direction",
        "flap_count",
        "suppressed_by_device_id",
        "suppress_reason",
        "aggregated_count",
    ):
        assert column in joined, f"4.6.0 缺少列 {column}"

    assert "alert_rules" in joined
    assert "alerts" in joined


def test_destructive_statements_are_scoped():
    """迁移里的 DELETE 必须带 WHERE，避免误清全表（4.3.4 的重复行清理是唯一先例）。"""
    for version, statements in MIGRATIONS.items():
        for sql in statements:
            upper = sql.upper()
            if "DELETE FROM" in upper:
                assert "WHERE" in upper, f"{version} 的 DELETE 缺少 WHERE 条件"


def test_column_additions_are_idempotent():
    """新增列一律用 IF NOT EXISTS，保证迁移可重复执行不报错。"""
    for version, statements in MIGRATIONS.items():
        for sql in statements:
            upper = sql.upper()
            if "ADD COLUMN" in upper:
                assert "IF NOT EXISTS" in upper, f"{version} 的 ADD COLUMN 缺少 IF NOT EXISTS"


@pytest.mark.parametrize("version", ["4.3.4", "4.3.5", "4.6.0"])
def test_previously_released_versions_still_present(version):
    """已发布版本不得被删除或改名——线上库靠版本号判断是否执行过。"""
    assert version in MIGRATIONS
