"""URL 延迟测量脚本

类似 ping 的实时延迟测量工具
用法: python latency_test.py <url> [-c 次数]
"""

import argparse
import asyncio
import signal
import sys
import time
from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class LatencyResult:
    """单次请求的延迟结果"""
    latency_ms: float
    status_code: int
    success: bool
    error: Optional[str] = None


class LatencyTester:
    """延迟测试器"""

    def __init__(self, url: str, timeout: float = 30.0):
        self.url = url
        self.timeout = timeout
        self.results: list[float] = []
        self.failed = 0
        self.running = True

    async def ping_once(self, client: httpx.AsyncClient, seq: int) -> None:
        """发送一次请求并打印结果"""
        start_time = time.perf_counter()
        try:
            response = await client.get(self.url)
            latency_ms = (time.perf_counter() - start_time) * 1000

            if 200 <= response.status_code < 400:
                self.results.append(latency_ms)
                print(f"来自 {self.url}: 状态={response.status_code} 延迟={latency_ms:.2f}ms seq={seq}")
            else:
                self.failed += 1
                print(f"来自 {self.url}: 状态={response.status_code} (异常) 延迟={latency_ms:.2f}ms seq={seq}")

        except httpx.TimeoutException:
            self.failed += 1
            print(f"请求超时: {self.url} seq={seq}")
        except httpx.ConnectError as e:
            self.failed += 1
            print(f"连接失败: {self.url} seq={seq} 错误={e}")
        except Exception as e:
            self.failed += 1
            print(f"请求失败: {self.url} seq={seq} 错误={e}")

    def print_statistics(self) -> None:
        """打印统计信息"""
        total = len(self.results) + self.failed
        print(f"\n--- {self.url} 延迟统计 ---")
        print(f"发送 {total} 个请求, 成功 {len(self.results)} 个, 失败 {self.failed} 个, 丢包率 {self.failed/total*100:.1f}%")

        if self.results:
            avg = sum(self.results) / len(self.results)
            min_lat = min(self.results)
            max_lat = max(self.results)

            # 计算标准差
            if len(self.results) > 1:
                variance = sum((x - avg) ** 2 for x in self.results) / len(self.results)
                stddev = variance ** 0.5
            else:
                stddev = 0

            print(f"延迟 最小={min_lat:.2f}ms 平均={avg:.2f}ms 最大={max_lat:.2f}ms 标准差={stddev:.2f}ms")

    async def run(self, count: Optional[int] = None, interval: float = 1.0) -> None:
        """
        运行延迟测试

        Args:
            count: 测试次数，None 表示无限
            interval: 请求间隔（秒）
        """
        print(f"PING {self.url} 间隔 {interval}s")
        print("-" * 50)

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)) as client:
            seq = 0
            try:
                while self.running:
                    if count is not None and seq >= count:
                        break

                    await self.ping_once(client, seq)
                    seq += 1

                    if count is None or seq < count:
                        await asyncio.sleep(interval)

            except asyncio.CancelledError:
                pass

        self.print_statistics()


async def main():
    parser = argparse.ArgumentParser(
        description="URL 延迟测量工具 - 类似 ping",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python latency_test.py https://www.google.com
  python latency_test.py https://api.example.com -c 10
  python latency_test.py https://example.com -c 5 -i 0.5
        """
    )
    parser.add_argument("url", help="目标 URL")
    parser.add_argument("-c", "--count", type=int, default=None, help="请求次数 (默认: 无限, Ctrl+C 停止)")
    parser.add_argument("-i", "--interval", type=float, default=1.0, help="请求间隔秒数 (默认: 1.0)")
    parser.add_argument("-t", "--timeout", type=float, default=30.0, help="超时时间秒数 (默认: 30.0)")

    args = parser.parse_args()

    # 确保 URL 有协议前缀
    url = args.url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    tester = LatencyTester(url, timeout=args.timeout)

    # 处理 Ctrl+C
    def signal_handler(sig, frame):
        print("\n^C")
        tester.running = False

    signal.signal(signal.SIGINT, signal_handler)

    await tester.run(count=args.count, interval=args.interval)


if __name__ == "__main__":
    asyncio.run(main())
