r"""按 NodeId 快速读取 KEPServer，独立运行，不写节点或项目数据库。

用法（仓库根目录）：
  backend\.venv\Scripts\python.exe backend\scripts\read_kep_node.py
  backend\.venv\Scripts\python.exe backend\scripts\read_kep_node.py "ns=2;s=Channel.Device.Tag"
  backend\.venv\Scripts\python.exe backend\scripts\read_kep_node.py "ns=2;s=Channel.Device.Tag" --interval 1
  backend\.venv\Scripts\python.exe backend\scripts\read_kep_node.py "ns=2;i=3" --interval 1 --count 5

不提供 NodeId 时循环交互输入；读完可继续输入，输入 q 或 exit 退出。
默认使用现场已验证的匿名 / None 端点。--endpoint 可指定其他服务器。
退出码：0=读数质量均为 Good / 用户停止，1=连接或服务错误，2=节点或质量问题。
"""

import argparse
import asyncio
import json
import logging
import math
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit


DEFAULT_ENDPOINT = "opc.tcp://10.255.1.21:49320"


def positive_number(value):
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("请输入大于 0 的秒数") from exc
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("秒数必须是大于 0 的有限数字")
    return result


def positive_count(value):
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("次数必须是大于 0 的整数") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("次数必须是大于 0 的整数")
    return result


def arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="只读 KEPServer NodeId：显示数值、类型、质量和时间戳。",
        epilog='NodeId 含空格时加双引号，例如 "ns=2;s=通道 1.4.501*XASH_1/if_prod"。',
    )
    parser.add_argument("node_ids", nargs="*", metavar="NODEID", help="一个或多个完整 NodeId；不填则循环交互输入，q 退出")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help=f"OPC UA 地址（默认 {DEFAULT_ENDPOINT}）")
    parser.add_argument("--interval", type=positive_number, metavar="SECONDS", help="连续读取，每轮完成后等待指定秒数；Ctrl+C 停止")
    parser.add_argument("--count", type=positive_count, metavar="N", help="读取轮数；未指定 interval 时轮间等待 1 秒")
    parser.add_argument("--timeout", type=positive_number, default=8.0, metavar="SECONDS", help="连接、读取、断开的超时秒数（默认 8）")
    args = parser.parse_args(argv)
    try:
        endpoint = urlsplit(args.endpoint)
        if endpoint.scheme != "opc.tcp" or not endpoint.hostname or not endpoint.port:
            raise ValueError
        if endpoint.username is not None or endpoint.password is not None:
            raise ValueError
    except ValueError:
        parser.error("endpoint 应为 opc.tcp://主机:端口，不能包含登录凭据")
    args.interactive = not args.node_ids
    if args.count is None and args.interval is None:
        args.count = 1
    if args.interval is None:
        args.interval = 1.0
    return args


def timestamp(value):
    if value is None:
        return "未提供"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().isoformat(timespec="milliseconds")


def display(node_id, data_value, received_at):
    variant = data_value.Value
    value = variant.Value if variant is not None else None
    value_type = variant.VariantType.name if variant is not None else "Null"
    status = data_value.StatusCode
    is_good = status.is_good()
    print(f"\nNodeId：{node_id}")
    print(f"  数值：{json.dumps(value, ensure_ascii=False, default=str)}")
    print(f"  类型：{value_type}")
    print(f"  质量：{status.name} (0x{status.value:08X})" + ("" if is_good else "，读数不可作为有效测量"))
    print(f"  源时间：{timestamp(data_value.SourceTimestamp)}")
    print(f"  服务器时间：{timestamp(data_value.ServerTimestamp)}")
    print(f"  接收时间：{timestamp(received_at)}", flush=True)
    return is_good


async def read(args):
    try:
        from asyncua import Client, ua
    except ImportError:
        print("缺少 asyncua。请用当前 Python 安装 backend/requirements-kepserver.txt。", file=sys.stderr)
        return 1
    # 不导入 app；不加载数据库、采集配置、认证凭据或私钥。
    logging.getLogger("asyncua").setLevel(logging.CRITICAL)
    parsed_nodes = []
    for node_id in args.node_ids:
        try:
            parsed_nodes.append(ua.NodeId.from_string(node_id))
        except (ValueError, ua.UaError) as exc:
            print(f"NodeId 格式错误：{node_id}（{type(exc).__name__}）", file=sys.stderr)
            return 2

    client = Client(args.endpoint, timeout=args.timeout)
    exit_code = 0
    try:
        print(f"连接：{args.endpoint}（匿名 / None，只读）", flush=True)
        async with asyncio.timeout(args.timeout):
            await client.connect()
        print("连接成功。时间戳按本机时区显示。", flush=True)
        rounds = 0
        while args.count is None or rounds < args.count:
            rounds += 1
            print(f"\n--- 第 {rounds} 轮 ---", flush=True)
            for offset in range(0, len(parsed_nodes), 100):
                batch = parsed_nodes[offset:offset + 100]
                request = ua.ReadParameters()
                request.MaxAge = 0
                request.TimestampsToReturn = ua.TimestampsToReturn.Both
                request.NodesToRead = [
                    ua.ReadValueId(NodeId=node_id, AttributeId=ua.AttributeIds.Value)
                    for node_id in batch
                ]
                async with asyncio.timeout(args.timeout):
                    values = await client.uaclient.read(request)
                received_at = datetime.now(timezone.utc)
                for node_id, data_value in zip(args.node_ids[offset:offset + 100], values, strict=True):
                    if not display(node_id, data_value, received_at):
                        exit_code = 2
            if args.count is None or rounds < args.count:
                await asyncio.sleep(args.interval)
        return exit_code
    except (TimeoutError, OSError, ua.UaError) as exc:
        if isinstance(exc, TimeoutError):
            message = f"超时（{args.timeout:g} 秒），请检查网络、服务器端口和 OPC UA 服务"
        elif isinstance(exc, ua.UaStatusCodeError):
            message = str(exc)
        else:
            message = f"{type(exc).__name__}: {exc}"
        print(f"连接或读取失败：{message}", file=sys.stderr, flush=True)
        return 1
    finally:
        try:
            async with asyncio.timeout(args.timeout):
                await client.disconnect()
        except Exception:
            client.disconnect_socket()
            print("连接清理未完成；服务器会在会话超时后回收。", file=sys.stderr)


def interactive(args):
    while True:
        try:
            node_id = input("\n请输入完整 NodeId（q 退出）：").strip()
        except EOFError:
            return 0
        if node_id.lower() in {"q", "exit"}:
            return 0
        if not node_id:
            continue
        args.node_ids = [node_id]
        asyncio.run(read(args))


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        args = arguments(argv)
        return interactive(args) if args.interactive else asyncio.run(read(args))
    except KeyboardInterrupt:
        print("\n已停止读取。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
