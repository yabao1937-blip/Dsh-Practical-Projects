"""极简 UA 仿真服务器：ns=2 Density=1.45，打印 SERVER_READY。用法：python ua_stub_server.py <port>"""
import asyncio
import sys


async def main():
    port = int(sys.argv[1])
    from asyncua import Server

    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://127.0.0.1:{port}/")
    server.set_security_policy([__import__("asyncua.ua", fromlist=["SecurityPolicyType"]).SecurityPolicyType.NoSecurity])
    idx = await server.register_namespace("urn:dmcs:test")
    obj = await server.nodes.objects.add_object(idx, "Test")
    node = await obj.add_variable(__import__("asyncua.ua", fromlist=["NodeId"]).NodeId("Density", idx), "Density", 1.45)
    async with server:
        print("SERVER_READY", flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
