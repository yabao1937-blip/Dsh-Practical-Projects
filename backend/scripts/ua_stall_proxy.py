"""UA 停转代理：先正常转发，到点后停止转发（连接保持打开），再恢复。

复现 2026-10-22 生产断采签名：KEP 的 TCP 连接在，但 UA 响应停止 → asyncua
ua_client.send_request 在 socket 仍 OPEN 时把 wait_for 超时包成裸 Exception
重抛（ua_client.py:221）→ 旧判定逻辑判 fatal。

用法：python ua_stall_proxy.py <listen_port> <target_host> <target_port> <stall_after_s> <stall_seconds>
输出：PROXY_READY / PROXY_STALL / PROXY_RESUME（行缓冲，供驱动脚本对时）
"""
import asyncio
import sys


async def main():
    listen_port = int(sys.argv[1])
    target_host = sys.argv[2]
    target_port = int(sys.argv[3])
    stall_after = float(sys.argv[4])
    stall_seconds = float(sys.argv[5])

    gate = asyncio.Event()
    gate.set()  # 初始放行

    async def lifecycle():
        await asyncio.sleep(stall_after)
        gate.clear()
        print("PROXY_STALL", flush=True)
        await asyncio.sleep(stall_seconds)
        gate.set()
        print("PROXY_RESUME", flush=True)

    async def relay(src, dst):
        while True:
            if not gate.is_set():
                await gate.wait()
            data = await src.read(65536)
            if not data:
                break
            dst.write(data)
            await dst.drain()

    async def handle(client_reader, client_writer):
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(target_host, target_port)
        except OSError:
            client_writer.close()
            return
        tasks = [asyncio.create_task(relay(client_reader, upstream_writer)),
                 asyncio.create_task(relay(upstream_reader, client_writer))]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for writer in (client_writer, upstream_writer):
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", listen_port)
    async with server:
        print("PROXY_READY", flush=True)
        await lifecycle()
        # 生命周期结束后继续保持转发，等待驱动脚本结束（由驱动杀进程）
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
