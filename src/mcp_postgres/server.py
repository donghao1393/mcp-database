"""PostgreSQL MCP 服务器实现"""

import asyncio
import sys
from typing import Optional
from contextlib import asynccontextmanager

import psycopg2
from psycopg2.pool import SimpleConnectionPool
from mcp.server import Server, NotificationOptions
import mcp.types as types
from mcp.server.stdio import stdio_server

from .config import PostgresConfig
from .log import create_logger

class PostgresServer:
    def __init__(self, config: PostgresConfig):
        self.config = config
        self.log = create_logger("postgres", config.debug)

        # 创建连接池
        try:
            conn_params = config.get_connection_params()
            self.log("info", f"正在连接数据库，参数: {conn_params}")

            # 测试连接
            test_conn = psycopg2.connect(**conn_params)
            test_conn.close()
            self.log("info", "测试连接成功")

            # 创建连接池
            self.pool = SimpleConnectionPool(1, 5, **conn_params)
            self.log("info", "数据库连接池创建成功")
        except psycopg2.Error as e:
            self.log("error", f"数据库连接失败: {str(e)}")
            raise

        # 初始化MCP服务器
        self.server = Server("postgres-server")
        self._setup_handlers()

    def _setup_handlers(self):
        @self.server.list_resources()
        async def handle_list_resources() -> list[types.Resource]:
            """列出所有表资源"""
            try:
                conn = self.pool.getconn()
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT 
                            table_name,
                            obj_description(
                                (quote_ident(table_schema) || '.' || quote_ident(table_name))::regclass,
                                'pg_class'
                            ) as description
                        FROM information_schema.tables 
                        WHERE table_schema = 'public'
                    """)
                    tables = cur.fetchall()

                    return [
                        types.Resource(
                            uri=f"postgres://{self.config.host}/{table[0]}/schema",
                            name=f"{table[0]} schema",
                            description=table[1] if table[1] else None,
                            mimeType="application/json"
                        ) for table in tables
                    ]
            except psycopg2.Error as e:
                self.log("error", f"获取表列表失败: {str(e)}")
                raise
            finally:
                self.pool.putconn(conn)

        @self.server.read_resource()
        async def handle_read_resource(uri: str) -> str:
            """读取表结构信息"""
            try:
                table_name = uri.split('/')[-2]
                conn = self.pool.getconn()
                with conn.cursor() as cur:
                    # 获取列信息
                    cur.execute("""
                        SELECT 
                            column_name,
                            data_type,
                            is_nullable,
                            col_description(
                                (quote_ident(table_schema) || '.' || quote_ident(table_name))::regclass,
                                ordinal_position
                            ) as description
                        FROM information_schema.columns 
                        WHERE table_name = %s
                        ORDER BY ordinal_position
                    """, (table_name,))
                    columns = cur.fetchall()

                    # 获取约束信息
                    cur.execute("""
                        SELECT
                            conname as constraint_name,
                            contype as constraint_type
                        FROM pg_constraint c
                        JOIN pg_class t ON c.conrelid = t.oid
                        WHERE t.relname = %s
                    """, (table_name,))
                    constraints = cur.fetchall()

                    return str({
                        'columns': [{
                            'name': col[0],
                            'type': col[1],
                            'nullable': col[2] == 'YES',
                            'description': col[3]
                        } for col in columns],
                        'constraints': [{
                            'name': con[0],
                            'type': con[1]
                        } for con in constraints]
                    })
            except psycopg2.Error as e:
                self.log("error", f"读取表结构失败: {str(e)}")
                raise
            finally:
                self.pool.putconn(conn)

        @self.server.list_tools()
        async def handle_list_tools() -> list[types.Tool]:
            """列出可用工具"""
            return [
                types.Tool(
                    name="query",
                    description="执行只读SQL查询",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "sql": {
                                "type": "string",
                                "description": "SQL查询语句（仅支持SELECT）"
                            }
                        },
                        "required": ["sql"]
                    }
                )
            ]

        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
            """处理工具调用"""
            if name != "query":
                raise ValueError(f"未知工具: {name}")

            sql = arguments.get("sql", "").strip()
            if not sql:
                raise ValueError("SQL查询不能为空")

            # 仅允许SELECT语句
            if not sql.lower().startswith("select"):
                raise ValueError("仅支持SELECT查询")

            self.log("info", f"执行查询: {sql}")

            try:
                conn = self.pool.getconn()
                with conn.cursor() as cur:
                    # 启动只读事务
                    cur.execute("BEGIN TRANSACTION READ ONLY")
                    try:
                        cur.execute(sql)
                        results = cur.fetchall()
                        columns = [desc[0] for desc in cur.description]

                        formatted_results = [dict(zip(columns, row)) for row in results]
                        result_text = str({
                            'columns': columns,
                            'rows': formatted_results,
                            'row_count': len(results)
                        })

                        self.log("info", f"查询完成，返回{len(results)}行结果")
                        return [types.TextContent(type="text", text=result_text)]
                    finally:
                        cur.execute("ROLLBACK")
            except psycopg2.Error as e:
                error_msg = f"查询执行失败: {str(e)}"
                self.log("error", error_msg)
                return [types.TextContent(type="text", text=error_msg)]
            finally:
                self.pool.putconn(conn)

    async def run(self):
        """运行服务器"""
        async with stdio_server() as streams:
            await self.server.run(
                streams[0],
                streams[1],
                self.server.create_initialization_options()
            )

    def cleanup(self):
        """清理资源"""
        if hasattr(self, 'pool'):
            self.log("info", "关闭数据库连接池")
            self.pool.closeall()

async def main():
    """主入口函数"""
    if len(sys.argv) < 2:
        print("用法: python -m mcp_postgres <database_url> [local_host]", file=sys.stderr)
        sys.exit(1)

    database_url = sys.argv[1]
    local_host = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        config = PostgresConfig.from_url(database_url, local_host)
        server = PostgresServer(config)
        await server.run()
    except KeyboardInterrupt:
        print("\n服务器已停止", file=sys.stderr)
    finally:
        if 'server' in locals():
            server.cleanup()

if __name__ == "__main__":
    asyncio.run(main())