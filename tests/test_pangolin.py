#!/usr/bin/env python3
"""Тест: прямое подключение к Pangolin через asyncpg (пароль из .env)."""

import asyncio
import os

# Читаем .env
env = {}
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

DSN = f"postgresql://{env['PG_USER']}:{env['PG_PASSWORD']}@{env['PG_HOST']}:{env['PG_PORT']}/{env['PG_DATABASE']}"


async def main():
    print("=" * 60)
    print("Тест прямого подключения к Pangolin")
    print("=" * 60)
    print(f"Host: {env['PG_HOST']}:{env['PG_PORT']}")
    print(f"User: {env['PG_USER']}")
    print(f"DB:   {env['PG_DATABASE']}")
    print()

    try:
        import asyncpg

        conn = await asyncpg.connect(DSN)
        print("✅ Подключение установлено")

        # Проверяем таблицу store
        rows = await conn.fetch("SELECT schemaname, tablename FROM pg_tables WHERE tablename = 'store'")
        print(f"Таблицы store: {rows}")

        # Попытка запроса к store
        try:
            result = await conn.execute(
                "SELECT table_schema, table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY table_schema, table_name;"
            )

            rows = await result.fetchall()

            # Печатаем результат
            print("📋 Список таблиц в базе данных:")
            for schema, table in rows:
                print(f"  - {schema}.{table}")

            print("✅ Доступ к store есть")
        except Exception as e:
            print(f"❌ Ошибка доступа к store: {e.description()}")

        # Проверяем права схем
        schemas = await conn.fetch(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast')"
        )
        print(f"Схемы: {[r[0] for r in schemas]}")

        # Попытка создать временную таблицу
        try:
            await conn.execute("CREATE TEMP TABLE IF NOT EXISTS _test_pangolin (id int)")
            await conn.execute("INSERT INTO _test_pangolin VALUES (1)")
            print("✅ CREATE TEMP TABLE — OK")
        except Exception as e:
            print(f"❌ CREATE TEMP TABLE: {e}")

        await conn.close()
        print("✅ Подключение закрыто")

    except Exception as e:
        print(f"❌ Ошибка подключения: {e}")


if __name__ == "__main__":
    asyncio.run(main())
