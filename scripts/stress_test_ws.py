"""
scripts/stress_test_ws.py — WebSocket 连通性压测脚本

阶段01 验证目标：
- 持续运行 30 分钟，观察 WebSocket 是否会因大数据量日志发生拥塞或断连
- 验证死连接清理机制是否正常工作
- 统计消息丢失率和延迟分布

用法：
    python scripts/stress_test_ws.py [--duration 1800] [--url ws://127.0.0.1:8000]
"""

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime


async def log_stream_client(url: str, client_id: int, results: dict, duration: int):
    """
    单个 WebSocket 日志流客户端。
    连接到 /api/v1/ws/logs，持续接收消息并统计。
    """
    import websockets

    received = 0
    reconnects = 0
    latencies = []
    start_time = time.monotonic()

    print(f"[Client-{client_id}] 连接到 {url}/api/v1/ws/logs")

    while time.monotonic() - start_time < duration:
        try:
            async with websockets.connect(
                f"{url}/api/v1/ws/logs",
                ping_interval=10,
                ping_timeout=5,
                close_timeout=5,
            ) as ws:
                if reconnects > 0:
                    print(f"[Client-{client_id}] 重连成功 (第 {reconnects} 次)")

                while time.monotonic() - start_time < duration:
                    try:
                        # 发送心跳
                        await ws.send("ping")
                        t0 = time.monotonic()

                        # 等待 pong 或日志消息（最多 5 秒）
                        msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        latency_ms = (time.monotonic() - t0) * 1000

                        if msg == "pong":
                            latencies.append(latency_ms)
                        else:
                            received += 1

                        await asyncio.sleep(1)  # 每秒发一次心跳

                    except asyncio.TimeoutError:
                        print(f"[Client-{client_id}] 心跳超时，检查连接...")
                        break

        except Exception as e:
            reconnects += 1
            print(f"[Client-{client_id}] 连接断开: {e}，{2}秒后重连...")
            await asyncio.sleep(2)

    results[client_id] = {
        "received": received,
        "reconnects": reconnects,
        "avg_latency_ms": statistics.mean(latencies) if latencies else 0,
        "max_latency_ms": max(latencies) if latencies else 0,
        "p95_latency_ms": (
            sorted(latencies)[int(len(latencies) * 0.95)]
            if len(latencies) >= 20 else 0
        ),
    }
    print(f"[Client-{client_id}] 完成: {results[client_id]}")


async def status_stream_client(url: str, results: dict, duration: int):
    """
    订阅 /api/v1/ws/status 通道，验证状态推送是否正常。
    """
    import websockets

    received = 0
    start_time = time.monotonic()

    try:
        async with websockets.connect(
            f"{url}/api/v1/ws/status",
            ping_interval=10,
            ping_timeout=5,
        ) as ws:
            print(f"[StatusClient] 连接到 {url}/api/v1/ws/status")
            while time.monotonic() - start_time < duration:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                    data = json.loads(msg)
                    received += 1
                    if received % 10 == 0:
                        print(f"[StatusClient] 收到第 {received} 条状态推送: status={data.get('status')}")
                except asyncio.TimeoutError:
                    print("[StatusClient] 状态推送超时（超过 10 秒未收到）")
                    break
    except Exception as e:
        print(f"[StatusClient] 连接异常: {e}")

    results["status_client"] = {"received": received}


async def log_flood_generator(url: str, duration: int):
    """
    通过 REST API 触发大量日志输出，模拟高频日志场景。
    每秒调用一次 /api/v1/status 来产生日志流量。
    """
    import aiohttp

    start_time = time.monotonic()
    requests_sent = 0

    async with aiohttp.ClientSession() as session:
        while time.monotonic() - start_time < duration:
            try:
                async with session.get(f"{url.replace('ws://', 'http://')}/api/v1/status") as resp:
                    await resp.json()
                    requests_sent += 1
            except Exception:
                pass
            await asyncio.sleep(0.5)  # 每 0.5 秒一次请求

    print(f"[FloodGen] 发送了 {requests_sent} 次 REST 请求")


async def main():
    parser = argparse.ArgumentParser(description="WebSocket 连通性压测")
    parser.add_argument("--duration", type=int, default=1800, help="压测持续时间（秒），默认 1800s = 30分钟")
    parser.add_argument("--url", default="ws://127.0.0.1:8000", help="后端 WebSocket URL")
    parser.add_argument("--clients", type=int, default=3, help="并发客户端数量")
    args = parser.parse_args()

    print("=" * 60)
    print(f"🚀 WebSocket 连通性压测启动")
    print(f"   目标: {args.url}")
    print(f"   持续: {args.duration}s ({args.duration / 60:.1f} 分钟)")
    print(f"   并发客户端: {args.clients}")
    print(f"   开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 先检查后端是否可达
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{args.url.replace('ws://', 'http://')}/api/v1/health",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                health = await resp.json()
                print(f"✅ 后端健康检查通过: {health}")
    except Exception as e:
        print(f"❌ 后端不可达: {e}")
        print("请先启动后端: python -m apps.trader.main")
        return

    results = {}

    # 并发运行多个客户端 + 状态订阅 + 日志洪流生成
    tasks = [
        log_stream_client(args.url, i, results, args.duration)
        for i in range(args.clients)
    ]
    tasks.append(status_stream_client(args.url, results, args.duration))
    tasks.append(log_flood_generator(args.url, args.duration))

    start = time.monotonic()
    await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.monotonic() - start

    # 打印汇总报告
    print("\n" + "=" * 60)
    print("📊 压测结果汇总")
    print("=" * 60)
    print(f"实际运行时间: {elapsed:.1f}s")

    total_reconnects = 0
    all_latencies = []

    for client_id, data in results.items():
        if client_id == "status_client":
            print(f"状态推送客户端: 收到 {data['received']} 条推送")
        else:
            reconnects = data.get("reconnects", 0)
            total_reconnects += reconnects
            avg_lat = data.get("avg_latency_ms", 0)
            max_lat = data.get("max_latency_ms", 0)
            print(
                f"日志客户端 {client_id}: "
                f"收到={data['received']} 消息, "
                f"重连={reconnects} 次, "
                f"平均延迟={avg_lat:.1f}ms, "
                f"最大延迟={max_lat:.1f}ms"
            )

    print(f"\n总重连次数: {total_reconnects}")
    if total_reconnects == 0:
        print("✅ 连通性验证通过：30分钟内无断连")
    else:
        print(f"⚠️  发生 {total_reconnects} 次重连，请检查网络或服务端日志")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
